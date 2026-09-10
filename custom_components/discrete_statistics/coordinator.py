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
changes them. A part hour - a window edge inside a compiled hour - is
read from the compiler's timeline of that hour while the recorder holds
it, and pro-rated from the hour's compiled change afterwards; the
timelines are kept per hour until a compile.

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
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import template
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import rows
from .compiler import Compiler, Timeline, compiled_signal
from .config import EntityConfig
from .const import DOMAIN, HOUR, METRIC_DURATION, SUBENTRY_SENSOR
from .periods import custom_window
from .reading import (
    Frame,
    Partial,
    PartialValue,
    Reading,
    Spec,
    compute,
    edges_of,
    exact_partial,
    plan,
    prorate,
    spec_from,
)
from .statistic_ids import parse

_LOGGER = logging.getLogger(__name__)

# How long a run of state changes is gathered into one refresh.
REFRESH_COOLDOWN = 2.0
# The reason a sensor whose window could not be rendered is unavailable.
REASON_TEMPLATE = "template"


def render_datetime(hass: HomeAssistant, text: str) -> float:
    """A template's rendering as a timestamp, or ValueError saying why not.

    What `history_stats` accepts: a date and time, or a timestamp. The
    flow renders once on submit with this same call, so a template that
    saves is one that renders - as long as what it reads still exists.
    """
    try:
        rendered = template.Template(text, hass).async_render(parse_result=False)
    except TemplateError as err:
        raise ValueError(str(err)) from err
    if (parsed := dt_util.parse_datetime(rendered)) is not None:
        return dt_util.as_utc(parsed).timestamp()
    try:
        return float(rendered)
    except ValueError:
        raise ValueError(f"{rendered!r} is not a date and time") from None


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
        self._hours: dict[float, Timeline] = {}
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
    def _compiled(self, start: float, end: float) -> None:
        # The frame is stale whatever was written. A sum is cumulative, so
        # one at an edge before the rewritten range is what it was, and one
        # after it is not: a finished window keeps its sums across every
        # hourly compile, and only a recompute reaching back drops them.
        # New objects, not mutation, for the refresh in flight - see
        # `_async_update_data`. Hour timelines go regardless: a recompile
        # after a mapping change reads the same rows differently.
        self._frame = None
        self._sums = {k: v for k, v in self._sums.items() if k[1] <= start}
        self._hours = {}
        self.config_entry.async_create_task(self.hass, self.async_refresh())

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        # Only ask: the debouncer gathers a burst of changes into the one
        # refresh, which is what keeps a chatty entity from draining the
        # recorder once per row it writes.
        self.config_entry.async_create_task(self.hass, self.async_request_refresh())

    def _window(self, spec: Spec, now: float) -> tuple[float, float] | str:
        """A custom period's window from its templates, or why it has none."""
        assert spec.custom is not None
        try:
            start = (
                None
                if spec.custom.start is None
                else render_datetime(self.hass, spec.custom.start)
            )
            end = (
                None
                if spec.custom.end is None
                else render_datetime(self.hass, spec.custom.end)
            )
        except ValueError as err:
            return f"{REASON_TEMPLATE}: {err}"
        window = custom_window(start, end, spec.custom.duration, now)
        if window is None:
            return f"{REASON_TEMPLATE}: two of start, end and duration are needed"
        return window

    async def _partial(
        self,
        cfg: EntityConfig,
        frame: Frame,
        sums: dict[tuple[str, float], float],
        hours: dict[float, Timeline],
        partial: Partial,
    ) -> PartialValue:
        """A part hour: exact while the recorder holds the hour, pro-rated after.

        The evidence is the entity's oldest retained state: at or before
        the hour, the compiler's timeline for the hour is the compile's
        own reading of it, cut at the edge. Otherwise the hour's compiled
        change is scaled by the part inside the window; a hole has a
        change of zero and contributes nothing, as a hole does everywhere.
        """
        if frame.earliest is not None and frame.earliest <= partial.hour:
            if (timeline := hours.get(partial.hour)) is None:
                timeline = await self._compiler.async_tail(
                    cfg, partial.hour, partial.hour + HOUR
                )
                if timeline is not None:
                    hours[partial.hour] = timeline
            if timeline is not None:
                return exact_partial(partial, timeline)
        seconds: dict[str, float] = {}
        counts: dict[str, float] = {}
        for statistic_id in frame.existing:
            if (parts := parse(statistic_id)) is None:
                continue
            token, metric = parts[1], parts[2]
            change = sums.get((statistic_id, partial.hour + HOUR), 0.0) - sums.get(
                (statistic_id, partial.hour), 0.0
            )
            if metric == METRIC_DURATION:
                seconds[token] = seconds.get(token, 0.0) + change * HOUR
            else:
                counts[token] = counts.get(token, 0.0) + change
        return prorate(partial, seconds, counts)

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
        # Not before Home Assistant has started: the recorder holds its
        # queue until then, and startup is waiting on this entry's setup,
        # so a refresh that waited here would wait on itself. The compile
        # at startup signals a refresh that drains.
        if self.hass.state is CoreState.running:
            await get_instance(self.hass).async_block_till_done()

        now = dt_util.utcnow().timestamp()
        tz = dt_util.get_default_time_zone()

        # A compile can land in any of the awaits below, and `_compiled`
        # answers it by dropping the frame and putting fresh caches in
        # place. Everything read here predates that compile, so it is
        # written to the cache objects this refresh started with: a stale
        # sum installed in the new cache would be read as a hit and hide
        # the trailing window's rewrite of that hour until the compile
        # after it.
        sums = self._sums
        hours = self._hours
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
            earliest = await self._compiler.async_earliest_state_ts(cfg.entity_id)
            frame = Frame(
                existing,
                None if watermark is None else watermark + HOUR,
                series_start,
                earliest,
            )
            if self._sums is sums:
                self._frame = frame

        # Every custom window is rendered here, once per refresh; a sensor
        # whose templates do not render is unavailable with the reason.
        readings: dict[str, Reading] = {}
        windows: dict[str, tuple[float, float] | None] = {}
        for subentry_id, spec in specs.items():
            if spec.custom is None:
                windows[subentry_id] = None
                continue
            window = self._window(spec, now)
            if isinstance(window, str):
                readings[subentry_id] = Reading(None, None, None, window)
                continue
            windows[subentry_id] = window
        wanted = {k: v for k, v in specs.items() if k not in readings}

        plans = {
            subentry_id: plan(spec, frame, now, tz, windows[subentry_id])
            for subentry_id, spec in wanted.items()
        }

        # The tail is read for the sensors whose window reaches it, not
        # for every live one: a finished window costs no read at all.
        timeline = None
        if frame.watermark_end is not None and any(
            spec.live and plans[subentry_id] is not None and plans[subentry_id].tail
            for subentry_id, spec in wanted.items()
        ):
            timeline = await self._compiler.async_tail(cfg, frame.watermark_end, now)

        edges: set[float] = set()
        for pieces in plans.values():
            edges |= edges_of(pieces)
        for edge in edges:
            if all((sid, edge) in sums for sid in frame.existing):
                continue
            at_edge = await get_instance(self.hass).async_add_executor_job(
                rows.sums_at, self.hass, set(frame.existing), edge
            )
            for statistic_id in frame.existing:
                sums[(statistic_id, edge)] = at_edge.get(statistic_id, 0.0)

        partials: dict[Partial, PartialValue] = {}
        for pieces in plans.values():
            if pieces is None:
                continue
            for partial in pieces.partials:
                if partial not in partials:
                    partials[partial] = await self._partial(
                        cfg, frame, sums, hours, partial
                    )

        def sum_at(statistic_id: str, edge: float) -> float:
            return sums.get((statistic_id, edge), 0.0)

        for subentry_id, spec in wanted.items():
            readings[subentry_id] = compute(
                cfg,
                spec,
                frame,
                sum_at,
                partials.get,
                timeline,
                now,
                tz,
                windows[subentry_id],
            )
        return readings
