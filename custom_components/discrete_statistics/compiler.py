"""Compile recorder history into external statistics."""

from __future__ import annotations

import functools as ft
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

        With no watermark - a new entity, or one whose statistics have all
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
        earliest = await self._async_earliest_state_ts(cfg.entity_id)
        if earliest is None:
            return 0
        window_start = hour_start(earliest if start is None else start)
        # Only completed hours are emitted.
        window_end = hour_start(end if end is not None else dt_util.utcnow().timestamp())
        if window_end <= window_start:
            return 0

        await async_warm_state_translations(self._hass, cfg.entity_id)
        existing = await self._async_existing(cfg.entity_id)
        if window_start < (evidence := first_whole_hour(earliest)):
            window_start = await self._async_opening_floor(
                existing, window_start, evidence
            )
            if window_end <= window_start:
                return 0

        base_sums, previous_hour = await self._async_base(existing, window_start)
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
                )
                compiled += hours
                chunk_start = chunk_end
        finally:
            # async_add_external_statistics only enqueues, and density is
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
        opened = self._open_window(cfg, rows, chunk_start, chunk_end, state)
        if opened is None:
            return state, 0
        window_start, carried, transitions = opened

        sums = state.sums
        if window_start != chunk_start:
            # The window moved past hours no source could open, so the base
            # was read for the wrong hour. Reading again is safe here and
            # only here: a window moves only when no state was carried into
            # it, and once a chunk has written anything the next one is
            # handed the state it ended in - so nothing is queued yet.
            sums, _ = await self._async_base(state.existing, window_start)

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
            sums,
            state.existing,
            display=display_name(self._hass, cfg.entity_id, cfg.name),
            translate=state_translator(self._hass, cfg.entity_id),
        )

        next_sums = dict(sums)
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
        from history entirely, and the whole span would go uncompiled -
        most visibly for the quiet entities this integration is most useful
        for.

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
    ) -> tuple[float, str, list[tuple[float, str]]] | None:
        """Decide where the window opens and in what state.

        Returns (window_start, carried, transitions), or None when there is
        nothing to compile. Deliberately synchronous: every source it
        consults is already to hand, which is what makes the order below
        safe to reason about.

        The window may open later than asked. When no source can say what
        state it opened in, the hours until the first one that begins in a
        known state are not compiled at all: they keep whatever rows they
        have, or none.
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

        if transitions and transitions[0] == (window_start, carried):
            # A row on the boundary into the state already carried is that
            # state's beginning, not a transition into it: the state
            # machine's `last_changed` IS this row, or an ignored row sat
            # between two spells of it. Counting it would count a change
            # from a state to itself.
            transitions = transitions[1:]

        if carried is not None:
            return window_start, carried, transitions

        # Open at the first whole hour whose state is known instead, paying
        # the partial hour before it: a part-known hour cannot both be
        # recorded and total wall-clock time. Whether the hours passed over
        # hold rows or not, the base is read again for the new start, so
        # the sums continue from the last row before it either way.
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
        assert carried is not None
        return window_start, carried, transitions

    async def _async_opening_floor(
        self, existing: Collection[str], window_start: float, evidence: float
    ) -> float:
        """Raise a window that opens before the recorder's evidence.

        Hours before the first whole hour of retained history have no rows
        to rebuild them from. Compiling them anyway does not describe the
        entity's past, it replaces it: when our own last row vouches for a
        state, every span falls to that one state and every real sum
        flattens to its base. A recompute reaching past the purge horizon
        therefore leaves those hours as they were compiled when the rows
        still existed.

        Unless they never were. Downtime longer than the horizon leaves a
        hole between the watermark and the evidence. The floor is the hour
        after the watermark, or the evidence, whichever comes first, which
        puts the watermark hour where the carry chain's fourth source reads
        it: a hole our last row can vouch for is filled with that state,
        and one it cannot is left open.
        """
        watermark = await self._async_watermark(existing)
        if watermark is None:
            return max(window_start, evidence)
        return max(window_start, min(evidence, watermark + HOUR))

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

    async def _async_base(
        self, statistic_ids: Collection[str], window_start: float
    ) -> tuple[dict[str, float], dict[str, float]]:
        """Return the sums the window continues from, and the previous hour's values.

        Both come from one query of the hour before the window. The values
        are how much of that hour each statistic accounted for, which is
        what `_carried_from_statistics` reads.

        A statistic with no row in that hour is looked for further back.
        The hour before a window is empty on the far side of a hole - hours
        no source could open, so never compiled - and the sum must carry
        across it, or the series restarts at zero and every chart shows
        the drop. The base is the newest row *before* the window, never the
        newest row: a recompute opening inside a hole has rows on both
        sides of it, and the ones ahead are what it is about to overwrite.
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
        for statistic_id in statistic_ids:
            if statistic_id in sums:
                continue
            before = await self._async_newest_sum_before(statistic_id, window_start)
            if before is not None:
                sums[statistic_id] = before
        return sums, values

    async def _async_newest_sum_before(
        self, statistic_id: str, window_start: float
    ) -> float | None:
        """The newest sum a statistic holds for an hour before window_start.

        The newest row overall answers when it precedes the window, which
        is every case but one: a recompute that opens inside a hole, with
        rows on both sides of it. Only then is the history before the
        window scanned - hourly rows, from the beginning. That path is
        rare enough to pay for the scan rather than bound it.
        """
        instance = get_instance(self._hass)
        result = await instance.async_add_executor_job(
            get_last_statistics, self._hass, 1, statistic_id, True, {"sum"}
        )
        rows = result.get(statistic_id)
        if not rows or rows[0].get("sum") is None:
            return None
        if rows[0]["start"] < window_start:
            return rows[0]["sum"]
        result = await instance.async_add_executor_job(
            statistics_during_period,
            self._hass,
            EPOCH,
            _as_datetime(window_start),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = [row for row in result.get(statistic_id, []) if row.get("sum") is not None]
        return rows[-1]["sum"] if rows else None

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
        starts before it. The part-hour is dropped for the same reason any
        window opens on a whole hour - a part-known hour cannot both be
        recorded and total wall-clock time.
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
