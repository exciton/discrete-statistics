"""The entry's sensors: period sensors, and the filtered-state sensor.

Each belongs to a subentry of the entry that records the entity, so
removing the subentry removes the sensor, and the entry itself still has
no entities.

A period sensor is a number read out of the statistics, one per `sensor`
subentry. The values come from the entry's coordinator; the entity holds
its spec, its name and how to present the metric. The recorder cannot be
told to skip these from here - `entity_filter` is the user's own
`recorder:` config - so what this module can do is give a tick nothing to
write: the value is rounded to what is shown, the attributes change only
when the period or the watermark does, and all of them are unrecorded.
The README asks for `sensor.discrete_*` to be excluded outright. A
rolling or custom window's value and edges do change every minute - the
window moves through recorded time - and that is the feature; `estimated`
says when a part hour of it was scaled from the hour's total rather than
read.

The filtered-state sensor is the `state` subentry, at most one per entry,
and shares none of that machinery: it reads the state machine, never the
recorder, so it answers in the time it takes an event to arrive rather
than behind a refresh and a queue drain. It is deliberately outside the
`sensor.discrete_` prefix, because unlike the period sensors it is worth
recording - it writes a row only when the entity's recorded state
actually changes.
"""

from __future__ import annotations

import logging
import math

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    CONF_ENTITY_ID,
    CONF_NAME,
    MATCH_ALL,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTime,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .config import EntityConfig, entity_config_from_entry
from .const import (
    DOMAIN,
    METRIC_DURATION,
    METRIC_SHARE,
    SUBENTRY_SENSOR,
    SUBENTRY_STATE,
)
from .coordinator import PeriodCoordinator
from .filtered import Tracker
from .filtered import suggested_entity_id as filtered_entity_id
from .naming import EntityNaming, entity_naming
from .periods import is_custom, is_rolling
from .reading import Reading, Spec, spec_from, suggested_entity_id

_LOGGER = logging.getLogger(__name__)

# The coordinator serialises the reads; there is nothing to parallelise.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one sensor per `sensor` and `state` subentry of the entry."""
    coordinator: PeriodCoordinator | None = None
    sensors: dict[str, DiscreteStatisticsSensor] = {}
    states: dict[str, FilteredStateSensor] = {}

    async def started() -> PeriodCoordinator:
        # Built on the first sensor and kept after the last one goes: the
        # coordinator listens to the entity's every state change, which is
        # work an entry with no sensors on it should not be doing, and a
        # sensor may well come back.
        nonlocal coordinator
        if coordinator is None:
            coordinator = PeriodCoordinator(hass, entry, hass.data[DOMAIN]["compiler"])
        # Not async_config_entry_first_refresh: a read that fails must not
        # fail the entry, which records regardless. The sensors are
        # unavailable until a refresh succeeds.
        await coordinator.async_refresh()
        return coordinator

    @callback
    def add(coordinator: PeriodCoordinator, subentry: ConfigSubentry) -> None:
        sensor = DiscreteStatisticsSensor(hass, coordinator, entry, subentry)
        sensors[subentry.subentry_id] = sensor
        async_add_entities([sensor], config_subentry_id=subentry.subentry_id)

    @callback
    def add_state(cfg: EntityConfig, subentry: ConfigSubentry) -> None:
        sensor = FilteredStateSensor(hass, cfg, entry, subentry)
        states[subentry.subentry_id] = sensor
        async_add_entities([sensor], config_subentry_id=subentry.subentry_id)

    def wanted(subentry_type: str) -> dict[str, ConfigSubentry]:
        return {
            subentry_id: subentry
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type == subentry_type
        }

    # Read from the entry rather than from `entry_configs`, so this does not
    # depend on which update listener runs first.
    def configured() -> EntityConfig:
        return entity_config_from_entry(entry.data, entry.options)

    if initial := wanted(SUBENTRY_SENSOR):
        started_coordinator = await started()
        for subentry in initial.values():
            add(started_coordinator, subentry)
    for subentry in wanted(SUBENTRY_STATE).values():
        add_state(configured(), subentry)

    async def updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Fires for every change to the entry. Diff the subentries against
        # the sensors: a removed one has already lost its entity to the
        # registry, a changed one is told, a new one is read before it is
        # added so it never shows as unavailable first.
        cfg = configured()
        current_states = wanted(SUBENTRY_STATE)
        for subentry_id in list(states):
            if subentry_id not in current_states:
                states.pop(subentry_id)
        for subentry_id, subentry in current_states.items():
            if subentry_id in states:
                # An options change never reloads the entry - it is applied
                # in place, so the history keeps no seam at the edit - which
                # means the dispositions can move under a live sensor and
                # this is the only place it hears about it.
                states[subentry_id].apply(hass, cfg, subentry)
            else:
                add_state(cfg, subentry)

        current = wanted(SUBENTRY_SENSOR)
        for subentry_id in list(sensors):
            if subentry_id not in current:
                sensors.pop(subentry_id)
        new = [s for sid, s in current.items() if sid not in sensors]
        # Every sensor is told, not just up to the first that reports a
        # change: `apply` is the telling, and one event can carry two
        # changed subentries.
        changed = False
        for subentry_id, subentry in current.items():
            if subentry_id in sensors:
                changed = sensors[subentry_id].apply(hass, subentry) or changed
        if not new and not changed:
            return
        refreshed = await started()
        for subentry in new:
            add(refreshed, subentry)

    entry.async_on_unload(entry.add_update_listener(updated))


def _iso(timestamp: float | None, whole_minutes: bool = False) -> str | None:
    if timestamp is None:
        return None
    if whole_minutes:
        timestamp = math.floor(timestamp / 60) * 60
    return dt_util.utc_from_timestamp(timestamp).isoformat()


class DiscreteStatisticsSensor(CoordinatorEntity[PeriodCoordinator], SensorEntity):
    """One number: a metric of some states over a period."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: PeriodCoordinator,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = subentry.subentry_id
        self._source = entry.data[CONF_ENTITY_ID]
        self._spec: Spec | None = None
        self._naming: EntityNaming | None = None
        self._warned = False
        # Set before adding: the platform takes it as the suggested object
        # id. A reconfigure keeps the entity, and so this id, since the
        # unique id is the subentry's.
        self.entity_id = suggested_entity_id(self._source, spec_from(subentry.data))
        # `hass` is an argument because the platform sets it on the entity
        # only after the add, and the add is what writes `device_id`.
        self.apply(hass, subentry)

    def apply(self, hass: HomeAssistant, subentry: ConfigSubentry) -> bool:
        """Take the subentry's spec and naming. True when either changed."""
        spec = spec_from(subentry.data)
        naming = entity_naming(
            hass, self._source, subentry.title, subentry.data.get(CONF_NAME)
        )
        changed = spec != self._spec or naming != self._naming
        self._spec = spec
        self._naming = naming
        self.device_entry = naming.device
        self._attr_has_entity_name = naming.has_entity_name
        self._attr_name = naming.name
        if spec.metric == METRIC_DURATION:
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = UnitOfTime.HOURS
        else:
            self._attr_device_class = None
            self._attr_native_unit_of_measurement = (
                PERCENTAGE if spec.metric == METRIC_SHARE else None
            )
        return changed

    @property
    def _reading(self) -> Reading | None:
        data = self.coordinator.data
        return None if data is None else data.get(self._attr_unique_id)

    @property
    def available(self) -> bool:
        reading = self._reading
        return super().available and reading is not None and reading.value is not None

    @property
    def native_value(self) -> float | int | None:
        reading = self._reading
        return None if reading is None else reading.value

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        reading = self._reading
        if reading is None:
            return {}
        assert self._spec is not None
        # A moving window's edges are shown to the minute, so they change
        # with the value rather than with each refresh's seconds.
        moving = is_rolling(self._spec.period) or is_custom(self._spec.period)
        return {
            "period_start": _iso(reading.period_start, moving),
            "period_end": _iso(reading.period_end, moving),
            "compiled_until": _iso(self.coordinator.compiled_until),
            "live": self._spec.live,
            "estimated": reading.estimated,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        # An unavailable entity carries no attributes, so the reason is
        # logged instead - once, when it first happens.
        reading = self._reading
        if reading is not None and reading.reason is not None:
            if not self._warned:
                _LOGGER.warning("%s is unavailable: %s", self.entity_id, reading.reason)
                self._warned = True
        else:
            self._warned = False
        super()._handle_coordinator_update()


def _restorable(cfg: EntityConfig, state: str | None) -> str | None:
    """The restored state, if this configuration could have produced it.

    A restore hands back whatever stood last, and after a shutdown that
    removed the entity that can be `unknown` or `unavailable` - neither of
    which this sensor writes itself while they resolve to nothing. Taking
    one back would report exactly the state the entry's settings say is
    not recorded.

    Asked of the configuration rather than hard-coded, because the two are
    not always unrecordable: a `states:` entry mapping onto `unavailable`
    makes it a map target, and a map target is recorded whatever the
    default says (`config.py:115`). So the test is whether a raw state of
    that name resolves to itself, which is what a canonical state of that
    name means.
    """
    if state is None:
        return None
    if state in (STATE_UNKNOWN, STATE_UNAVAILABLE) and cfg.classify(state)[0] != state:
        return None
    return state


class FilteredStateSensor(RestoreEntity, SensorEntity):
    """The state the entity is in, as this entry records it.

    Mapped, filtered and debounced by the entry's own settings, so an
    automation can trigger on the same timeline the statistics are built
    from rather than re-encoding the disposition table in a template.

    Never unavailable because the entity it follows is - that is the point
    of it. Under `record_known` a device dropping off the network is
    carried across, and this holds the state it was in, so an automation
    does not fire on a reload, a restart or a router reboot. The value is
    only ever a state `classify` returned, so a state the entry ignores
    can never appear here; before anything recordable has been seen it
    reads Unknown, which is not the same claim as Unavailable.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    # No device class. `SensorDeviceClass.ENUM` wants an exhaustive
    # `options`, and the recordable states are an open set unless the
    # entry ignores by default: under `record` any state the entity has
    # not reported yet is a valid one.
    _attr_device_class = None
    _attr_native_unit_of_measurement = None

    def __init__(
        self,
        hass: HomeAssistant,
        cfg: EntityConfig,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
    ) -> None:
        self._tracker: Tracker | None = None
        self._unsubscribe: CALLBACK_TYPE | None = None
        self._timer: CALLBACK_TYPE | None = None
        self._attr_unique_id = subentry.subentry_id
        self._source = entry.data[CONF_ENTITY_ID]
        self._naming: EntityNaming | None = None
        # Set before adding: the platform takes it as the suggested object
        # id. A reconfigure keeps the entity and so this id, since the
        # unique id is the subentry's.
        self.entity_id = filtered_entity_id(self._source)
        # `hass` is an argument because the platform sets it on the entity
        # only after the add, and the add is what writes `device_id`.
        self.apply(hass, cfg, subentry)

    async def async_added_to_hass(self) -> None:
        """Open the tracker on what is known, then follow the entity."""
        await super().async_added_to_hass()
        self.async_on_remove(self._stop)
        restored = await self.async_get_last_state()
        self._start(
            _restorable(self._cfg, None if restored is None else restored.state)
        )

    @callback
    def apply(
        self, hass: HomeAssistant, cfg: EntityConfig, subentry: ConfigSubentry
    ) -> None:
        """Take the subentry's naming, and rebuild on a change of settings.

        The dispositions decide what every raw state means, so a change to
        them invalidates the spell in progress and the state carried into
        it alike: the tracker is opened again on the entity as it stands
        now. The carried state survives only if the new settings could
        have produced it, which is the same question `_restorable` answers
        for a restore.
        """
        naming = entity_naming(
            hass, self._source, subentry.title, subentry.data.get(CONF_NAME)
        )
        self._naming = naming
        self.device_entry = naming.device
        self._attr_has_entity_name = naming.has_entity_name
        self._attr_name = naming.name
        if self._tracker is None:
            self._cfg = cfg
            return
        if cfg != self._cfg:
            carried = _restorable(cfg, self._attr_native_value)
            self._cfg = cfg
            self._start(carried)
        self.async_write_ha_state()

    @callback
    def _start(self, carried: str | None) -> None:
        """Open a tracker on `carried` and subscribe to the entity.

        The entity's current state goes through `observe` like any other
        row rather than being taken as the answer: at startup it is often
        `unavailable`, and under `record_known` that is precisely the value
        this sensor promises never to report. Its own `last_changed` is
        used rather than now, so a state that has been in effect for an
        hour is not measured as a spell that began this second.
        """
        self._stop()
        self._tracker = Tracker(self._cfg, carried)
        source = self.hass.states.get(self._cfg.entity_id)
        if source is not None:
            self._tracker.observe(source.state, source.last_changed.timestamp())
        self._unsubscribe = async_track_state_change_event(
            self.hass, [self._cfg.entity_id], self._changed
        )
        self._settle()

    @callback
    def _stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._timer is not None:
            self._timer()
            self._timer = None

    @callback
    def _changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        # `new_state` is None when the entity leaves the state machine - a
        # YAML reload, an integration unloading, a removal. The recorder
        # stores NULL for that and the history API reads it back as "", so
        # a compile covering this moment sees a blank state; `filtered`
        # translates it the same way and `blank:` decides the rest. The
        # event's own time stands in for the `last_changed` of a state
        # object there is not one of.
        assert self._tracker is not None
        self._tracker.observe(
            None if new_state is None else new_state.state,
            event.time_fired.timestamp()
            if new_state is None
            else new_state.last_changed.timestamp(),
        )
        self._settle()

    @callback
    def _settle(self) -> None:
        """Take the answer, and arm the timer a pending spell needs.

        Every ignored state of a chatty entity reaches this, and none of
        them should cost a row. The state machine is what guarantees that
        - `async_set` drops a write whose state and attributes both match
        what stands - so the attributes here are constant and the value is
        only ever a state `classify` returned. The check below is just not
        building the attributes to have them thrown away.
        """
        assert self._tracker is not None
        if self._timer is not None:
            self._timer()
            self._timer = None
        now = dt_util.utcnow().timestamp()
        value = self._tracker.state(now)
        if value != self._attr_native_value:
            self._attr_native_value = value
            self.async_write_ha_state()
        if (until := self._tracker.pending_until(now)) is not None:
            self._timer = async_call_later(
                self.hass, max(until - now, 0.0), self._matured
            )

    @callback
    def _matured(self, _now: object) -> None:
        # Nothing arrives when a short spell becomes long enough to
        # record, so this is the only thing that reports it. `_settle`
        # arms the timer again if this one fired a hair early.
        self._timer = None
        self._settle()
