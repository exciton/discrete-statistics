"""One refresh per config entry, shared by every period sensor on it.

A refresh drains the recorder's write queue, then reads what it holds
for the entity once - its statistics, the watermark, the series start -
then the sums at each edge the sensors between them ask for, then one
live tail from the watermark end, and computes every sensor from those.
The drain belongs to the refresh rather than to any one of its triggers,
so whichever of them asked reads rows that are already written. It runs
after each compile (the compiler's dispatcher signal), on each change of
the entity's state and once a minute for the live tail's clock, and the
sums it has read are kept until the next compile, since nothing else
changes them.

A state change asks for a refresh through the coordinator's debouncer
rather than taking one: the entity may be chatty, and a drain per change
commits the recorder's session for the whole instance. A change is
reflected within `REFRESH_COOLDOWN` of it instead.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import rows
from .compiler import Compiler, compiled_signal
from .const import DOMAIN, HOUR, SUBENTRY_SENSOR
from .reading import Frame, Reading, compute, edges, spec_from

_LOGGER = logging.getLogger(__name__)

# How long a run of state changes is gathered into one refresh.
REFRESH_COOLDOWN = 2.0


class PeriodCoordinator(DataUpdateCoordinator[dict[str, Reading]]):
    """Readings keyed by sensor subentry id."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, compiler: Compiler
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=60),
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=REFRESH_COOLDOWN, immediate=False
            ),
        )
        self._compiler = compiler
        self._frame: Frame | None = None
        self._sums: dict[tuple[str, float], float] = {}
        entity_id = entry.data[CONF_ENTITY_ID]
        entry.async_on_unload(
            async_dispatcher_connect(hass, compiled_signal(entity_id), self._compiled)
        )
        entry.async_on_unload(
            async_track_state_change_event(hass, [entity_id], self._state_changed)
        )

    @property
    def compiled_until(self) -> float | None:
        """The end of the newest compiled hour, once read."""
        return None if self._frame is None else self._frame.watermark_end

    @callback
    def _compiled(self) -> None:
        # The statistics changed: everything read from them is stale.
        self._frame = None
        self._sums = {}
        self.config_entry.async_create_task(self.hass, self.async_refresh())

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        # Only ask: the debouncer gathers a burst of changes into the one
        # refresh, which is what keeps a chatty entity from draining the
        # recorder once per row it writes.
        self.config_entry.async_create_task(self.hass, self.async_request_refresh())

    async def _async_update_data(self) -> dict[str, Reading]:
        entry = self.config_entry
        cfg = self.hass.data[DOMAIN]["entry_configs"].get(entry.entry_id)
        if cfg is None:
            raise UpdateFailed(f"{entry.title} is not set up")
        specs = {
            subentry_id: spec_from(subentry.data)
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type == SUBENTRY_SENSOR
        }
        # The coordinator outlives the last sensor subentry, so a refresh
        # with nothing to compute reads nothing either.
        if not specs:
            return {}

        # The recorder is a write queue: drain it so the rows every read
        # below wants are there, whichever trigger asked for this refresh.
        await get_instance(self.hass).async_block_till_done()

        now = dt_util.utcnow().timestamp()
        tz = dt_util.get_default_time_zone()

        # A compile can land in any of the awaits below, and `_compiled`
        # answers it by dropping the frame and putting a fresh cache in
        # place. Everything read here predates that compile, so it is
        # written to the cache object this refresh started with: a stale
        # sum installed in the new cache would be read as a hit and hide
        # the trailing window's rewrite of that hour until the compile
        # after it.
        sums = self._sums
        frame = self._frame
        if frame is None:
            existing, watermark = await self._compiler.async_compiled(cfg.entity_id)
            series_start = (
                await get_instance(self.hass).async_add_executor_job(
                    rows.series_start, self.hass, set(existing)
                )
                if existing
                else None
            )
            frame = Frame(
                existing, None if watermark is None else watermark + HOUR, series_start
            )
            if self._sums is sums:
                self._frame = frame

        timeline = None
        if frame.watermark_end is not None and any(s.live for s in specs.values()):
            timeline = await self._compiler.async_tail(cfg, frame.watermark_end, now)

        wanted: set[float] = set()
        for spec in specs.values():
            wanted |= edges(spec, frame, now, tz)
        for edge in wanted:
            if all((sid, edge) in sums for sid in frame.existing):
                continue
            at_edge = await get_instance(self.hass).async_add_executor_job(
                rows.sums_at, self.hass, set(frame.existing), edge
            )
            for statistic_id in frame.existing:
                sums[(statistic_id, edge)] = at_edge.get(statistic_id, 0.0)

        def sum_at(statistic_id: str, edge: float) -> float:
            return sums.get((statistic_id, edge), 0.0)

        return {
            subentry_id: compute(cfg, spec, frame, sum_at, timeline, now, tz)
            for subentry_id, spec in specs.items()
        }
