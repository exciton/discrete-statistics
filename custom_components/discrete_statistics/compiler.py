"""Compile recorder history into external statistics."""

from __future__ import annotations

import functools as ft
import logging
from collections.abc import Collection, Mapping
from datetime import datetime, timezone
from typing import NamedTuple

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .bucketer import bucket, first_whole_hour, hour_start
from .canonicalise import canonicalise
from .config import EntityConfig
from .const import DOMAIN, HOUR, METRIC_DURATION
from .naming import async_warm_state_translations, display_name, state_translator
from .payload import build_payloads
from .statistic_ids import belongs_to, parse, state_token

_LOGGER = logging.getLogger(__name__)

# Recompute this many trailing hours on every run, so a state committed by
# the recorder after we first read its hour is still picked up.
TRAILING_HOURS = 3

# Compile in windows of this size to bound memory during a long backfill.
CHUNK_HOURS = 24 * 7

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Both of `state_changes_during_period`'s queries compare strictly, so a
# state landing exactly on window_start is returned by neither. Querying from
# slightly earlier moves it into the changes half, where canonicalise folds
# it into the carried state.
#
# Half a second, not a whole one: window_start is always an exact multiple of
# 3600.0, so 1.0 would land on another integer second and move the hole
# rather than close it. Anything below a microsecond rounds straight back,
# datetime.fromtimestamp being microsecond-resolution.
START_MARGIN = 0.5

def _readable_state(stored_name: str, token: str) -> str:
    """Recover a state from the name its statistic already carries.

    An ID holds only the token, so a state carried out of one would read
    `heatcool` where the entity says `heat_cool` - and that name would then
    be written for as long as nothing transitioned. The stored name still
    has the readable form, in the half `rename` leaves alone.

    Verified rather than trusted: the recovered text must tokenise back to
    the same token, or the name did not have the shape assumed and the token
    stands.
    """
    head, separator, _ = stored_name.rpartition(" (")
    if not separator:
        return token
    _, separator, state = head.rpartition(": ")
    if separator and state_token(state) == token:
        return state
    return token


def _as_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


class _ChunkState(NamedTuple):
    """What one chunk hands the next. Threaded, never re-read.

    Every field is stale the moment it is queried again, because
    `async_add_external_statistics` only enqueues: mid-compile the previous
    chunk's rows and metadata are still in the recorder's write queue.

    `sums` are the cumulative bases the next chunk's rows continue from.
    Re-reading them would restart a series at zero and break monotonicity.

    `existing` is every statistic the entity has, including any this chunk
    created - which the recorder cannot report yet, and which the next chunk
    needs in order to stay dense.

    `carried` is the state in effect at the chunk's end, which the next
    chunk opens in. It is also simply more accurate than any query: the
    exact value is already to hand.
    """

    sums: dict[str, float]
    existing: dict[str, str]
    carried: str | None


def _carried_from_statistics(
    values: Mapping[str, float], names: Mapping[str, str]
) -> str | None:
    """The state a uniform previous hour was spent in, if there was one.

    Our own rows already encode the carry-forward decision - an hour spent
    `unavailable` under `record_known` was written as the state carried into
    it, not as a gap - so they are the resolved timeline, which is exactly
    what a lookback into raw history is trying to reconstruct. And density
    guarantees a row for that hour however long the entity has been quiet,
    so there is no distance limit.

    Only when one duration statistic accounts for the hour. Several mean
    transitions happened inside it, so the recorder has rows there and
    `include_start_time_state` finds them: the two sources answer disjoint
    questions.
    """
    held = [
        (statistic_id, parts[1])
        for statistic_id, value in values.items()
        if value
        and (parts := parse(statistic_id)) is not None
        and parts[2] == METRIC_DURATION
    ]
    if len(held) != 1:
        return None
    statistic_id, token = held[0]
    return _readable_state(names.get(statistic_id, ""), token)


class Compiler:
    """Compile one entity's history into statistics."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def _async_existing(self, entity_id: str) -> dict[str, str]:
        """Return {statistic_id: stored name} for one entity's statistics.

        The recorder's metadata is the only record of which statistics an
        entity has - `belongs_to` recovers the association from the ID - so a
        statistic the user deleted is absent here and stops being written.
        """
        metadata = await get_instance(self._hass).async_add_executor_job(
            ft.partial(get_metadata, self._hass, statistic_source=DOMAIN)
        )
        return {
            statistic_id: (meta["name"] or "")
            for statistic_id, (_, meta) in metadata.items()
            if belongs_to(statistic_id, entity_id)
        }

    async def async_compile_incremental(self, cfg: EntityConfig) -> int:
        """Compile from the watermark, recomputing the trailing window.

        With no watermark - a new helper, or one whose statistics have all
        been deleted - there is nothing to trail, so it compiles the whole
        of the entity's retained history instead.
        """
        existing = await self._async_existing(cfg.entity_id)
        watermark = await self._async_watermark(existing)
        if watermark is None:
            start = await self._async_earliest_state_ts(cfg.entity_id)
            if start is None:
                return 0
        else:
            start = watermark - (TRAILING_HOURS - 1) * HOUR
        # `existing` is deliberately NOT handed on. It looks like a wasted
        # query, but this read happens before _async_watermark's round-trips
        # and only the later one in async_compile reliably reflects a
        # statistic deleted moments earlier. Merging them makes the deletion
        # tests flaky one run in three, and a stale view leaves statistics
        # sparse, which cannot be repaired.
        return await self.async_compile(cfg, start)

    async def async_compile(
        self, cfg: EntityConfig, start: float | None, end: float | None = None
    ) -> int:
        """Compile [start, end) for one entity. Returns hours compiled."""
        # `start is None` means "from the beginning of what the recorder
        # still holds", so nothing precedes the window and the leading
        # no_data may be trimmed even though statistics already exist.
        # An explicit start is a range the caller asked for, and reaching
        # back past what is known is then a real gap worth showing.
        at_earliest_state = start is None
        if start is None:
            start = await self._async_earliest_state_ts(cfg.entity_id)
            if start is None:
                return 0

        window_start = hour_start(start)
        # Only completed hours are emitted.
        window_end = hour_start(end if end is not None else dt_util.utcnow().timestamp())
        if window_end <= window_start:
            return 0

        await async_warm_state_translations(self._hass, cfg.entity_id)
        existing = await self._async_existing(cfg.entity_id)
        base_sums, previous_hour = await self._async_previous_hour(
            existing, window_start
        )
        # Read once, for the hour this compile opens at. Every chunk after
        # the first takes the state from the one before it, for the reason
        # `_ChunkState` gives.
        state = _ChunkState(
            sums=base_sums,
            existing=existing,
            carried=_carried_from_statistics(previous_hour, existing),
        )

        compiled = 0
        chunk_start = window_start
        try:
            while chunk_start < window_end:
                chunk_end = min(chunk_start + CHUNK_HOURS * HOUR, window_end)
                state, hours = await self._async_compile_chunk(
                    cfg,
                    chunk_start,
                    chunk_end,
                    state,
                    first_chunk=chunk_start == window_start,
                    # Only the chunk that opens the history: a later one
                    # starts mid-series, where trimming would leave a hole.
                    opens_history=at_earliest_state and chunk_start == window_start,
                )
                compiled += hours
                chunk_start = chunk_end
        finally:
            # async_add_external_statistics only enqueues, and density is now
            # read live from statistics_meta. In `finally` because a chunk
            # that raises leaves earlier chunks' writes queued: the next
            # compile would then see half of them, leave the rest sparse, and
            # the window after that would restart those at zero.
            await get_instance(self._hass).async_block_till_done()

        return compiled

    async def _async_history(
        self, cfg: EntityConfig, query_start: float, window_end: float
    ) -> list:
        """Return the recorder rows for [query_start, window_end).

        The start-time state is included, so the first row is the state in
        effect at query_start whatever its timestamp.
        """
        history = await get_instance(self._hass).async_add_executor_job(
            state_changes_during_period,
            self._hass,
            _as_datetime(query_start - START_MARGIN),
            _as_datetime(window_end),
            cfg.entity_id,
            True,  # no_attributes
            False,  # descending
            None,  # limit
            True,  # include_start_time_state
        )
        return history.get(cfg.entity_id, [])

    async def _async_compile_chunk(
        self,
        cfg: EntityConfig,
        chunk_start: float,
        chunk_end: float,
        state: _ChunkState,
        *,
        first_chunk: bool,
        opens_history: bool,
    ) -> tuple[_ChunkState, int]:
        """Compile one chunk.

        Returns what the next chunk starts from and the hours actually
        compiled.
        """
        # An hour further back on the opening chunk.
        # `include_start_time_state` hands back exactly ONE row before the
        # boundary, so a single ignored row there hides a perfectly good
        # state behind it. Reading the previous hour whole means canonicalise
        # sees both and carries the good one forward. Later chunks need none
        # of this: they are handed the state the previous chunk ended in.
        rows = await self._async_history(
            cfg, chunk_start - HOUR if first_chunk else chunk_start, chunk_end
        )
        opened = self._open_window(
            cfg, rows, chunk_start, chunk_end, state, opens_history
        )
        if opened is None:
            return state, 0
        window_start, carried, transitions = opened

        buckets = bucket(carried, transitions, window_start, chunk_end)

        # Every statistic this entity already has must get a row in every
        # hour, even when this window saw nothing of its state. Otherwise the
        # next window finds no row in the hour before it, restarts that
        # statistic's cumulative sum from zero and loses the running total.
        payloads = build_payloads(
            cfg,
            buckets,
            window_start,
            chunk_end,
            state.sums,
            state.existing,
            display=display_name(self._hass, cfg.entity_id, cfg.name),
            translate=state_translator(self._hass, cfg.entity_id),
        )

        next_sums = dict(state.sums)
        next_existing = dict(state.existing)
        for statistic_id, (metadata, statistic_rows) in payloads.items():
            async_add_external_statistics(self._hass, metadata, statistic_rows)
            next_sums[statistic_id] = statistic_rows[-1]["sum"]
            next_existing[statistic_id] = metadata["name"]

        return (
            _ChunkState(
                sums=next_sums,
                existing=next_existing,
                # What the next chunk opens in: the last state this one reached.
                carried=transitions[-1][1] if transitions else carried,
            ),
            int((chunk_end - window_start) / HOUR),
        )

    def _carried_from_state_machine(
        self, cfg: EntityConfig, window_start: float
    ) -> str | None:
        """The live state, when it demonstrably held across the window.

        The recorder can hold nothing at all for an entity that has not
        changed within `purge_keep_days`: purge deletes every row past the
        horizon with no per-entity reprieve (`queries.py:281`). An entity
        that sits in one state longer than the horizon therefore disappears
        from history entirely, and the whole span would be attributed to
        no_data - most visibly for the quiet entities this integration is
        most useful for.

        The state machine still knows, and `last_changed` is what makes this
        sound rather than a guess: at or before `window_start` proves the
        state was already in effect then. After it, the state began inside
        the window and says nothing about how the window opened, so it is
        refused - which is also what keeps a backfill of old hours from
        being handed today's state.
        """
        state = self._hass.states.get(cfg.entity_id)
        if state is None or state.last_changed.timestamp() > window_start:
            return None
        return cfg.resolve(state.state)

    def _open_window(
        self,
        cfg: EntityConfig,
        rows: list,
        window_start: float,
        window_end: float,
        state: _ChunkState,
        opens_history: bool,
    ) -> tuple[float, str | None, list[tuple[float, str]]] | None:
        """Decide where the window opens and in what state.

        Returns (window_start, carried, transitions), or None when there is
        nothing to compile. Deliberately synchronous: every source it
        consults is already to hand, which is what makes the order below
        safe to reason about.
        """
        carried, transitions = canonicalise(cfg, rows, window_start)

        if carried is None:
            # Proof rather than inference - `last_changed` demonstrates the
            # state was already in effect - so it is tried before the carry.
            carried = self._carried_from_state_machine(cfg, window_start)

        if carried is None:
            # Nothing recordable in the previous hour either. The carry is
            # the state at this window's start as the previous chunk left it,
            # or - on the opening chunk - as that hour's own statistics
            # record it: a whole hour with nothing to record means the entity
            # held one state throughout, which is exactly what they say.
            carried = state.carried

        if carried is None and (not state.existing or opens_history):
            # Nothing has ever been compiled for this entity, so a leading
            # no_data would complete no earlier series - it would only earn
            # the entity a no_data statistic to write densely forever. Open
            # at the first whole hour whose state we know instead, paying the
            # whole first partial hour: a part-known hour cannot both be
            # recorded and total wall-clock time.
            #
            # Allowed when the entity has no statistics at all, or when this
            # window opens its history - `start=None`, which is what the
            # a bare `recompute` does. Both mean nothing precedes
            # the window, so moving its start cannot orphan a base. Trimming
            # a window that begins mid-series would: the hours skipped would
            # have no rows, and the next run would find no base in the hour
            # before it and restart every cumulative sum at zero.
            if not transitions:
                return None
            window_start = first_whole_hour(transitions[0][0])
            if window_start >= window_end:
                return None
            carried, transitions = canonicalise(cfg, rows, window_start)
            if transitions and transitions[0][0] == window_start:
                # Nothing transitioned INTO an entity's first known state, so
                # carry it rather than counting its birth as an event just
                # because it fell on the hour.
                carried = transitions[0][1]
                transitions = transitions[1:]
            return window_start, carried, transitions

        if carried is None:
            _LOGGER.warning(
                "No recoverable state for %s at %s; attributing the span to no_data",
                cfg.entity_id,
                _as_datetime(window_start).isoformat(),
            )
        return window_start, carried, transitions

    async def _async_watermark(self, statistic_ids: Collection[str]) -> float | None:
        """Return the newest compiled hour for an entity, or None.

        Takes the max across every one of the entity's statistics: density
        is guaranteed only for statistics that existed when a window was
        compiled, so any single ID can lag the others.
        """
        if not statistic_ids:
            return None
        newest: float | None = None
        for statistic_id in statistic_ids:
            result = await get_instance(self._hass).async_add_executor_job(
                get_last_statistics,
                self._hass,
                1,
                statistic_id,
                True,
                {"sum"},
            )
            if rows := result.get(statistic_id):
                start = rows[0]["start"]
                if newest is None or start > newest:
                    newest = start
        return newest

    async def _async_previous_hour(
        self, statistic_ids: Collection[str], window_start: float
    ) -> tuple[dict[str, float], dict[str, float]]:
        """Return the hour before the window: its cumulative sums, and its own values.

        Both come from one query. The sums are what the new rows continue
        from; the values are how much of that hour each statistic accounted
        for, which is what `_carried_from_statistics` reads.
        """
        if not statistic_ids:
            return {}, {}
        result = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            _as_datetime(window_start - HOUR),
            _as_datetime(window_start),
            set(statistic_ids),
            "hour",
            None,
            {"sum", "mean"},
        )
        sums = {
            statistic_id: rows[-1]["sum"]
            for statistic_id, rows in result.items()
            if rows and rows[-1].get("sum") is not None
        }
        values = {
            statistic_id: rows[-1]["mean"]
            for statistic_id, rows in result.items()
            if rows and rows[-1].get("mean") is not None
        }
        return sums, values

    async def _async_earliest_state_ts(self, entity_id: str) -> float | None:
        """Return the timestamp to open an entity's history at, or None.

        The oldest retained state, or - when the recorder holds nothing at
        all, because the entity has not changed within `purge_keep_days` -
        the first whole hour after the live state began. An entity with no
        history and no current state earns nothing; one with a current state
        earns the span it has demonstrably held it for.

        A whole hour, not `last_changed` itself, so that the window opens at
        a moment `_carried_from_state_machine` will vouch for: it requires
        `last_changed <= window_start`, and the hour containing the change
        starts before it. The part-hour is dropped for the same reason the
        leading no_data is - a part-known hour cannot both be recorded and
        total wall-clock time.
        """
        history = await get_instance(self._hass).async_add_executor_job(
            state_changes_during_period,
            self._hass,
            EPOCH,
            None,
            entity_id,
            True,
            False,
            1,
            False,
        )
        if rows := history.get(entity_id):
            return rows[0].last_changed_timestamp

        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        return first_whole_hour(state.last_changed.timestamp())
