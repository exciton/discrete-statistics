"""Period sensors: a number read out of the statistics, one per subentry.

Each sensor belongs to a `sensor` subentry of the entry that records the
entity, so removing the subentry removes the sensor, and the entry itself
still has no entities. The values come from the entry's coordinator; the
entity holds its spec, its name and how to present the metric.

The recorder cannot be told to skip these from here - `entity_filter`
is the user's own `recorder:` config - so what this module can do is
give a tick nothing to write: the value is rounded to what is shown,
the attributes change only when the period or the watermark does, and
all of them are unrecorded. The README asks for `sensor.discrete_*`
to be excluded outright.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_ENTITY_ID, MATCH_ALL, PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, METRIC_DURATION, METRIC_SHARE, SUBENTRY_SENSOR
from .coordinator import PeriodCoordinator
from .reading import Reading, Spec, spec_from, suggested_entity_id

_LOGGER = logging.getLogger(__name__)

# The coordinator serialises the reads; there is nothing to parallelise.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one period sensor per `sensor` subentry of the entry."""
    coordinator: PeriodCoordinator | None = None
    sensors: dict[str, DiscreteStatisticsSensor] = {}

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
        sensor = DiscreteStatisticsSensor(coordinator, entry, subentry)
        sensors[subentry.subentry_id] = sensor
        async_add_entities([sensor], config_subentry_id=subentry.subentry_id)

    def wanted() -> dict[str, ConfigSubentry]:
        return {
            subentry_id: subentry
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type == SUBENTRY_SENSOR
        }

    if initial := wanted():
        started_coordinator = await started()
        for subentry in initial.values():
            add(started_coordinator, subentry)

    async def updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Fires for every change to the entry. Diff the subentries against
        # the sensors: a removed one has already lost its entity to the
        # registry, a changed one is told, a new one is read before it is
        # added so it never shows as unavailable first.
        current = wanted()
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
                changed = sensors[subentry_id].apply(subentry) or changed
        if not new and not changed:
            return
        refreshed = await started()
        for subentry in new:
            add(refreshed, subentry)

    entry.async_on_unload(entry.add_update_listener(updated))


def _iso(timestamp: float | None) -> str | None:
    return (
        None if timestamp is None else dt_util.utc_from_timestamp(timestamp).isoformat()
    )


class DiscreteStatisticsSensor(CoordinatorEntity[PeriodCoordinator], SensorEntity):
    """One number: a metric of some states over a period."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        coordinator: PeriodCoordinator,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = subentry.subentry_id
        self._spec: Spec | None = None
        self._warned = False
        # Set before adding: the platform takes it as the suggested object
        # id. A reconfigure keeps the entity, and so this id, since the
        # unique id is the subentry's.
        self.entity_id = suggested_entity_id(
            entry.data[CONF_ENTITY_ID], spec_from(subentry.data)
        )
        self.apply(subentry)

    def apply(self, subentry: ConfigSubentry) -> bool:
        """Take the subentry's spec and title. True when either changed."""
        spec = spec_from(subentry.data)
        changed = spec != self._spec or subentry.title != self._attr_name
        self._spec = spec
        self._attr_name = subentry.title
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
        return {
            "period_start": _iso(reading.period_start),
            "period_end": _iso(reading.period_end),
            "compiled_until": _iso(self.coordinator.compiled_until),
            "live": self._spec.live,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        # An unavailable entity carries no attributes, so the reason is
        # logged instead - once, when it first happens.
        reading = self._reading
        if reading is not None and reading.reason is not None:
            if not self._warned:
                _LOGGER.warning(
                    "%s is unavailable: its states are %s",
                    self.entity_id,
                    reading.reason,
                )
                self._warned = True
        else:
            self._warned = False
        super()._handle_coordinator_update()
