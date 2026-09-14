"""Compile recorder history into external statistics."""

from __future__ import annotations

import asyncio
import functools as ft
import logging
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple

from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    import_statistics,
)
from homeassistant.components.recorder.tasks import (
    ImportStatisticsTask,
    RecorderTask,
    SynchronizeTask,
)
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError, OperationalError

from .bucketer import bucket, first_whole_hour, hour_start
from .canonicalise import canonicalise
from .config import EntityConfig
from .const import DOMAIN, HOUR, METRIC_COUNT, METRIC_DURATION
from .naming import async_warm_state_translations, display_name, state_translator
from .payload import build_payloads, partition_rows, readable_state
from .rows import bases, metadata_ids, series_end, standing
from .statistic_ids import belongs_to, parse, rehome

_LOGGER = logging.getLogger(__name__)

# How long the fence waits for the recorder to commit what a compile
# queued. Generous, because a backlogged recorder must never be cut short;
# bounded, because the await sits inside the compile lock and a recorder
# that stops serving its queue between the check and the task would
# otherwise hold that lock for the life of the process.
FENCE_TIMEOUT = 60

# Recompute this many trailing hours on every run, so a state committed by
# the recorder after we first read its hour is still picked up.
TRAILING_HOURS = 3

# Compile in windows of this size to bound memory during a long backfill.
CHUNK_HOURS = 24 * 7

# Rows per INSERT in the bulk path. SQLAlchemy sends a list of dicts to
# `cursor.executemany`, which binds one row at a time and so has no
# parameter limit to breach - but a driver that rewrites the batch into a
# single multi-row VALUES (SQLAlchemy's psycopg2 dialect does, under its
# default `executemany_mode`, at 1 000 rows a page) would, and SQLite's
# 32 766 bound variables is the tightest of those: thirteen columns a row
# puts the ceiling at ~2 500. Under it, and large enough that a busy
# entity's week is one statement.
BULK_ROWS = 2000

# The off switch. With it False every row goes back through
# `async_add_external_statistics`, which is what the equivalence test
# compiles against and what the bench measures "before" with.
BULK_INSERT = True

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


def _as_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


class _Stored(NamedTuple):
    """What the recorder holds for one statistic, of what a compile sets.

    Every field `payload.metadata_for` writes and nothing else, so an
    import that would change none of them can be skipped: a rename, a
    unit, a unit class, a mean type or the sum flag all still reach a
    statistic the window never saw, with no rows beside them.

    `has_mean` is the one field the recorder neither compares nor
    rewrites: `_update_metadata` leaves it out, and a read derives it from
    `mean_type`. So `payload.metadata_for` must keep the two consistent,
    or every rowless statistic would compare unequal and import on every
    compile.
    """

    name: str
    unit_of_measurement: str | None
    unit_class: str | None
    mean_type: StatisticMeanType
    has_sum: bool
    has_mean: bool


def _stored(meta: Mapping[str, Any]) -> _Stored:
    """One statistic's metadata as it compares, the recorder's or ours.

    The recorder answers with `None` for a name it never had, and the two
    flags with whatever the column holds, so both are normalised here
    rather than at each comparison.
    """
    return _Stored(
        name=meta.get("name") or "",
        unit_of_measurement=meta.get("unit_of_measurement"),
        unit_class=meta.get("unit_class"),
        mean_type=StatisticMeanType(meta.get("mean_type") or StatisticMeanType.NONE),
        has_sum=bool(meta.get("has_sum")),
        has_mean=bool(meta.get("has_mean")),
    )


def _names(stored: Mapping[str, _Stored]) -> dict[str, str]:
    """The display names alone, which is all `payload` is given."""
    return {statistic_id: meta.name for statistic_id, meta in stored.items()}


class _ChunkState(NamedTuple):
    """What one chunk hands the next. Threaded, never re-read.

    Every field is stale the moment it is queried again, because
    `async_add_external_statistics` only enqueues: mid-compile the previous
    chunk's rows and metadata are still in the recorder's write queue.

    `sums` are the cumulative bases the next chunk's rows continue from.
    Re-reading them would restart a series at zero and break monotonicity.

    `existing` is every statistic the entity has and the metadata it
    carries, including any this chunk created - which the recorder cannot
    report yet, and which the next chunk needs to relabel every statistic,
    to know which standing rows it must rewrite, and to tell an import
    that would change nothing from one that would.

    `carried` is the state in effect at the chunk's end, which the next
    chunk opens in. It is also simply more accurate than any query: the
    exact value is already to hand.
    """

    sums: dict[str, float]
    existing: dict[str, _Stored]
    carried: str | None


class Timeline(NamedTuple):
    """The resolved timeline of a window, as a compile would bucket it.

    What `async_tail` hands the period sensors: the state the window opened
    in and every canonical transition inside it, over exactly the rows,
    carry chain and dispositions a compile of the same window reads - so a
    sensor's live tail is what the next compile writes, a provisional
    `ignore_short` verdict included.
    """

    start: float
    carried: str
    transitions: list[tuple[float, str]]


def compiled_signal(entity_id: str) -> str:
    """The dispatcher signal sent after an entity's compile has written.

    Listeners receive `(window_start, window_end)`, the range written.
    """
    return f"{DOMAIN}_compiled_{entity_id}"


def _carried_from_statistics(
    values: Mapping[str, float], names: Mapping[str, str]
) -> str | None:
    """The state a uniform previous hour was spent in, if there was one.

    Our own rows already encode the carry-forward decision - an hour spent
    `unavailable` under `record_known` was written as the state carried into
    it, not as a gap - so they are the resolved timeline, which is exactly
    what a lookback into raw history is trying to reconstruct. And the state
    the hour was spent in always has a row there however long the entity has
    been quiet, so there is no distance limit.

    Only when exactly one duration statistic changed in the hour. Several
    mean transitions happened inside it, so the recorder has rows there and
    `include_start_time_state` finds them: the two sources answer disjoint
    questions. A row standing with no change is not a change.
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
    return readable_state(names.get(statistic_id, ""), token)


def _batches(
    values: Sequence[dict[str, Any]], size: int
) -> Iterator[Sequence[dict[str, Any]]]:
    """`values` in slices of at most `size`."""
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _row_values(metadata_id: int, row: Mapping[str, Any], now: float) -> dict[str, Any]:
    """One statistics row as `Statistics.from_stats` would build it.

    Every column of the table but the identity key, spelled out: a core
    INSERT compiles against the keys of the first dict, so a row that left
    one out would leave it out of the statement for the whole batch.
    """
    return {
        "metadata_id": metadata_id,
        # The legacy datetime columns. `from_stats` writes None into both
        # and the recorder reads only the `_ts` pair.
        "created": None,
        "created_ts": now,
        "start": None,
        "start_ts": row["start"].timestamp(),
        # A duration or a count statistic carries a sum and nothing else.
        "mean": None,
        "mean_weight": None,
        "min": None,
        "max": None,
        "last_reset": None,
        "last_reset_ts": None,
        "state": None,
        "sum": row["sum"],
    }


def _bulk_insert(
    instance: Recorder,
    payloads: Mapping[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> None:
    """Insert rows no row stands at, in one statement a batch. Recorder thread.

    Every batch shares one session and commits with it, so a failure in
    any of them discards them all - which is what lets the caller fall
    back for the whole task rather than for part of it. Nothing is caught
    here: the caller decides.

    The metadata ids are resolved here rather than carried, because only
    the recorder thread may ask: `statistics_meta_manager` keeps a cache
    it alone owns, and a statistic whose metadata was created by the
    import task queued just ahead of this one exists by the time this
    runs and not before.

    A statistic with no metadata is skipped, not created. The import that
    would have created it is ahead of this task in the same queue and
    retries itself, so arriving here without one means the metadata is
    gone rather than late - the user deleted the statistic while the
    compile ran - and recreating it would resurrect something nothing
    else records the deletion of. The next compile reads `existing`
    fresh and writes the rows again if the statistic is still there.
    """
    now = time.time()
    with session_scope(session=instance.get_session()) as session:
        found = instance.statistics_meta_manager.get_many(
            session, statistic_ids=set(payloads)
        )
        values: list[dict[str, Any]] = []
        for statistic_id, (_, rows) in payloads.items():
            if (meta := found.get(statistic_id)) is None:
                _LOGGER.warning(
                    "No metadata for %s; %s new rows not written",
                    statistic_id,
                    len(rows),
                )
                continue
            values.extend(_row_values(meta[0], row, now) for row in rows)
        for batch in _batches(values, BULK_ROWS):
            session.execute(insert(Statistics), batch)


@dataclass(slots=True)
class BulkInsertTask(RecorderTask):
    """Insert the rows one chunk proved were not there yet.

    The recorder's own import path runs a SELECT-exists and then an
    INSERT or an UPDATE per row, and commits per statistic - about 2.8 ms
    a row here, which is most of a long recompute. A row the compiler
    calls new needs none of that: `rows.standing` read every row our
    statistics hold across the chunk moments earlier, inside the same
    compile and under the same lock, and nothing but this integration
    writes them.

    `payloads` is {statistic_id: (metadata, rows)}. The metadata is
    carried only for the fallback below; the rows are `StatisticData` -
    a `start` and a `sum`.

    A batch the fallback cannot write either is lost: the hourly run
    never revisits it, because the watermark advances with the chunks
    that did succeed. `recompute` with a `start:` before the hole is
    what rebuilds it.
    """

    payloads: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]

    def run(self, instance: Recorder) -> None:
        """Handle the task."""
        try:
            _bulk_insert(instance, self.payloads)
        except (IntegrityError, OperationalError) as err:
            # An IntegrityError means a row stands where our read said
            # none did; an OperationalError is the database refusing the
            # statement. Either way losing the batch would leave every
            # later sum on a base that was never written, so the recorder's
            # own upsert runs instead - inline, because the compile's fence
            # is already queued behind this task and anything queued here
            # would commit after `async_compile` returned. Anything else
            # escapes to the recorder's own guard around the task.
            _LOGGER.warning(
                "Bulk insert of %s statistics failed with %s; "
                "falling back to the recorder's own import",
                len(self.payloads),
                type(err).__name__,
            )
            for metadata, rows in self.payloads.values():
                if not import_statistics(instance, metadata, rows, Statistics):
                    # Retryable, after the recorder's own wait. Queued
                    # again exactly as ImportStatisticsTask does - behind
                    # the fence, as the recorder's own import already is.
                    instance.queue_task(
                        ImportStatisticsTask(metadata, rows, Statistics)
                    )


class Renamed(NamedTuple):
    """What a rename moved, and what it could not."""

    moved: list[str]
    collided: list[str]


@dataclass(slots=True)
class RenameTask(RecorderTask):
    """Move an entity's statistics to another entity ID.

    `update_statistic_id` is recorder-thread only, which is why this is a
    task and not an executor job. The future resolves after the session
    has committed, because the caller's next step - updating the config
    entry, whose reload compiles under the new ID - reads this metadata:
    a compile that opened before the rename committed would find no
    watermark under the new name and rebuild the retained history as a
    second series.

    A statistic whose new ID already exists is left where it is. Two
    series are never merged; the caller reports it.
    """

    old_entity_id: str
    new_entity_id: str
    future: asyncio.Future[Renamed]

    def run(self, instance: Recorder) -> None:
        """Handle the task."""
        moved: list[str] = []
        collided: list[str] = []
        manager = instance.statistics_meta_manager
        try:
            with session_scope(session=instance.get_session()) as session:
                ours = metadata_ids(
                    session, (), [slugify(self.old_entity_id, separator="_")]
                )
                for statistic_id in sorted(ours):
                    new_id = rehome(statistic_id, self.new_entity_id)
                    if new_id is None:
                        continue
                    if manager.get(session, new_id):
                        collided.append(statistic_id)
                        continue
                    manager.update_statistic_id(session, DOMAIN, statistic_id, new_id)
                    moved.append(statistic_id)
        except Exception as err:  # noqa: BLE001 - the awaiting caller must not hang
            instance.hass.loop.call_soon_threadsafe(self.future.set_exception, err)
            return
        instance.hass.loop.call_soon_threadsafe(
            self.future.set_result, Renamed(moved, collided)
        )


class Compiler:
    """Compile one entity's history into statistics."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def _async_stored(self, entity_id: str) -> dict[str, _Stored]:
        """Return {statistic_id: the metadata the recorder holds} for an entity.

        The recorder's metadata is the only record of which statistics an
        entity has - `belongs_to` recovers the association from the ID - so a
        statistic the user deleted is absent here and stops being written.
        """
        metadata = await get_instance(self._hass).async_add_executor_job(
            ft.partial(get_metadata, self._hass, statistic_source=DOMAIN)
        )
        return {
            statistic_id: _stored(meta)
            for statistic_id, (_, meta) in metadata.items()
            if belongs_to(statistic_id, entity_id)
        }

    async def async_existing(self, entity_id: str) -> dict[str, str]:
        """Return {statistic_id: stored name} for one entity's statistics.

        The names alone, which is what every caller outside a compile
        wants: the compile itself compares the whole of the metadata.
        """
        return _names(await self._async_stored(entity_id))

    async def async_rename(self, old_entity_id: str, new_entity_id: str) -> Renamed:
        """Move every statistic of `old_entity_id` to `new_entity_id`.

        Returns once the rename is committed. See `RenameTask`.
        """
        future: asyncio.Future[Renamed] = self._hass.loop.create_future()
        get_instance(self._hass).queue_task(
            RenameTask(old_entity_id, new_entity_id, future)
        )
        return await future

    async def async_compile_incremental(self, cfg: EntityConfig) -> int:
        """Compile from the watermark, recomputing the trailing window.

        With no watermark - a new entity, or one whose statistics have all
        been deleted - there is nothing to trail, so it compiles the whole
        of the entity's retained history instead.
        """
        existing = await self._async_stored(cfg.entity_id)
        watermark = await self._async_watermark(existing)
        if watermark is None:
            start = await self.async_earliest_state_ts(cfg.entity_id)
            if start is None:
                return 0
        else:
            start = watermark - (TRAILING_HOURS - 1) * HOUR
        # `existing` is deliberately NOT handed on: this read happens
        # before `_async_watermark`'s own, and only the later read in
        # `async_compile` reliably reflects a statistic deleted moments
        # earlier. A stale view leaves a statistic unrelabelled and its
        # standing rows unrewritten.
        return await self.async_compile(cfg, start)

    async def async_compile(
        self,
        cfg: EntityConfig,
        start: float | None,
        end: float | None = None,
        *,
        read_from: str | None = None,
    ) -> int:
        """Compile [start, end) for one entity. Returns hours compiled.

        `read_from` names the entity whose recorded history is read - a
        replacement device's temporary ID, whose states the fill writes
        under `cfg.entity_id`. Only the recorder reads move with it: the
        statistics, and the state machine carry, are always the entity
        configured.
        """
        source = read_from or cfg.entity_id
        earliest = await self.async_earliest_state_ts(source)
        if earliest is None:
            return 0
        window_start = hour_start(earliest if start is None else start)
        # Only completed hours are emitted.
        window_end = hour_start(
            end if end is not None else dt_util.utcnow().timestamp()
        )
        if window_end <= window_start:
            return 0

        await async_warm_state_translations(self._hass, cfg.entity_id)
        existing = await self._async_stored(cfg.entity_id)
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
            carried=_carried_from_statistics(previous_hour, _names(existing)),
        )

        compiled = 0
        wrote = False
        chunk_start = window_start
        try:
            while chunk_start < window_end:
                chunk_end = min(chunk_start + CHUNK_HOURS * HOUR, window_end)
                state, hours, imported = await self._async_compile_chunk(
                    cfg,
                    chunk_start,
                    chunk_end,
                    state,
                    first_chunk=chunk_start == window_start,
                    read_from=source,
                )
                compiled += hours
                wrote = wrote or imported
                chunk_start = chunk_end
        finally:
            # async_add_external_statistics only enqueues, and every read
            # the next compile opens with - the base, the watermark, the
            # standing rows, the metadata - is live. In `finally` because a
            # chunk that raises leaves earlier chunks' writes queued: the
            # next compile would then see half of them, leave the rest
            # unrewritten, and the window after that would restart those at
            # zero.
            await self._async_fence()

        if wrote:
            # The sensors re-read after a compile rather than on a clock of
            # their own: the write is fenced above, so what they read now
            # is what was just written. The range says which of their
            # cached sums a rewrite could have moved.
            async_dispatcher_send(
                self._hass, compiled_signal(cfg.entity_id), window_start, window_end
            )

        return compiled

    async def async_fill(
        self, cfg: EntityConfig, source_entity_id: str, before: float
    ) -> int:
        """Compile a replacement entity's early history into our series.

        From the last real transition of ours - the newest row across the
        entity's count statistics, since the watermark marches on over an
        entity gone unavailable - to the last whole hour before the
        rename, reading the states under `source_entity_id`. The hour of
        the rename straddles both IDs and is left to the ordinary compile.

        Fenced first: the recorder's own rename listener runs ahead of
        ours and moves the states history onto our name when nothing of
        ours stands in its way, and only after its task has run can the
        recorder say which ID the history is under.
        """
        await self._async_fence()
        read_from = source_entity_id
        if await self.async_earliest_recorded_ts(source_entity_id) is None:
            read_from = cfg.entity_id
        # Read live rather than reusing a fence-adjacent view, for the same
        # reason `async_compile_incremental` re-reads before its own call:
        # a statistic deleted moments earlier must not be judged against a
        # stale metadata snapshot.
        existing = await self._async_stored(cfg.entity_id)
        counts = {
            statistic_id
            for statistic_id in existing
            if (parts := parse(statistic_id)) is not None and parts[2] == METRIC_COUNT
        }
        start = await self._async_watermark(counts)
        if start is None:
            start = await self.async_earliest_recorded_ts(read_from)
            if start is None:
                return 0
        end = hour_start(before)
        if end <= start:
            return 0
        return await self.async_compile(cfg, start, end, read_from=read_from)

    async def _async_fence(self) -> None:
        """Wait until everything this compile queued has been committed.

        Our own task at the back of the recorder's queue, and the queue is
        served in order, so it runs after the last import and bulk insert
        task has - and
        `commit_before` commits the session first.
        `Recorder.async_block_till_done` is not enough: it queues this same
        task only while the queue is non-empty, and the queue empties when
        the last import task is *popped*, which is before it has written
        anything. A compile that returned there could be followed within
        milliseconds - the `recompute` service behind the hourly run, the
        two serialised by the lock - by one whose base and watermark open
        behind the rows just written, and whose sums would then descend.
        """
        recorder = get_instance(self._hass)
        if not recorder.is_running:
            # Nothing is serving the queue - the thread has stopped, or has
            # not reached its loop - so the task would never run. The
            # writes stay queued for whoever starts it.
            return
        future = self._hass.loop.create_future()
        recorder.queue_task(SynchronizeTask(future))
        try:
            async with asyncio.timeout(FENCE_TIMEOUT):
                await future
        except TimeoutError:
            # The rows are enqueued either way, so raising here would lose
            # the compile rather than the write. The next compile may open
            # behind them, which is what the trailing window corrects.
            _LOGGER.warning(
                "Timed out waiting %ss for the recorder to commit what the "
                "compile wrote; continuing",
                FENCE_TIMEOUT,
            )

    async def async_tail(
        self, cfg: EntityConfig, start: float, end: float
    ) -> Timeline | None:
        """The timeline of [start, end) as a compile would read it. Writes nothing.

        `start` is an hour the sensors know is not compiled - the end of
        the watermark hour - so the carry is read as the opening chunk of a
        compile reads it, and the window may open later than asked for the
        reasons `_open_window` gives. None when no source can open it.
        """
        if end <= start:
            return None
        existing = await self._async_stored(cfg.entity_id)
        _, previous_hour = await self._async_base(existing, start)
        state = _ChunkState(
            sums={},
            existing=existing,
            carried=_carried_from_statistics(previous_hour, _names(existing)),
        )
        rows = await self._async_history(cfg, cfg.entity_id, start - HOUR, end)
        opened = self._open_window(cfg, rows, start, end, state)
        return None if opened is None else Timeline(*opened)

    async def async_compiled(
        self, entity_id: str
    ) -> tuple[dict[str, str], float | None]:
        """The entity's statistics and the newest hour compiled into them."""
        existing = await self.async_existing(entity_id)
        return existing, await self._async_watermark(existing)

    async def _async_history(
        self, cfg: EntityConfig, entity_id: str, query_start: float, window_end: float
    ) -> list:
        """Return the recorder rows of `entity_id` for [query_start, window_end).

        Plus `cfg.min_duration` beyond the end, so a short spell that ends
        after the window can still be measured.

        The start-time state is included, so the first row is the state in
        effect at query_start - and timestamped there, not where it
        actually began. A spell is therefore measured from the query start
        at the earliest, which is why `min_duration` is capped at the hour
        the opening chunk reads back: a spell that reaches the window from
        further back than that always measures long.
        """
        history = await get_instance(self._hass).async_add_executor_job(
            state_changes_during_period,
            self._hass,
            _as_datetime(query_start - START_MARGIN),
            _as_datetime(window_end + cfg.min_duration),
            entity_id,
            True,  # no_attributes
            False,  # descending
            None,  # limit
            True,  # include_start_time_state
        )
        return history.get(entity_id, [])

    async def _async_compile_chunk(
        self,
        cfg: EntityConfig,
        chunk_start: float,
        chunk_end: float,
        state: _ChunkState,
        *,
        first_chunk: bool,
        read_from: str,
    ) -> tuple[_ChunkState, int, bool]:
        """Compile one chunk.

        Returns what the next chunk starts from, the hours actually
        compiled, and whether anything was handed to the recorder.

        A payload with no rows whose metadata the recorder already holds,
        field for field, is not imported at all: a task, a commit and a
        metadata round trip for a statistic with nothing to say. Any
        difference - a rename, a unit, a mean type - is imported with no
        rows beside it, which is how every one of them reaches a statistic
        the window never saw.

        The rows themselves take two paths. A row at an hour `standing`
        found a row at is an upsert and goes through the recorder's own
        import, with the metadata; a row at an hour it found nothing at is
        new, and one `BulkInsertTask` for the whole chunk inserts every
        one of them in a statement a batch. The split is the standing read
        itself - the same evidence `build_payloads` wrote each row on -
        and it is sound because that read and this write are one compile,
        under the compile lock, and nothing outside this integration
        writes a `discrete_statistics:` statistic.
        """
        # An hour further back on the opening chunk.
        # `include_start_time_state` hands back exactly ONE row before the
        # boundary, so a single ignored row there hides a perfectly good
        # state behind it. Reading the previous hour whole means canonicalise
        # sees both and carries the good one forward. Later chunks need none
        # of this: they are handed the state the previous chunk ended in.
        rows = await self._async_history(
            cfg,
            read_from,
            chunk_start - HOUR if first_chunk else chunk_start,
            chunk_end,
        )
        opened = self._open_window(cfg, rows, chunk_start, chunk_end, state)
        if opened is None:
            return state, 0, False
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

        # A row that already stands in the window is rewritten even when
        # its state is absent now, or its old sum would stand ahead of
        # every later one; nothing deletes.
        stands = await get_instance(self._hass).async_add_executor_job(
            standing, self._hass, set(state.existing), window_start, chunk_end
        )
        payloads = build_payloads(
            cfg,
            buckets,
            window_start,
            chunk_end,
            sums,
            _names(state.existing),
            display=display_name(self._hass, cfg.entity_id, cfg.name),
            translate=state_translator(self._hass, cfg.entity_id),
            standing=stands,
        )

        next_sums = dict(sums)
        next_existing = dict(state.existing)
        imported = False
        fresh: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for statistic_id, payload in payloads.items():
            meta = _stored(payload.metadata)
            if BULK_INSERT:
                new_rows, rewrites = partition_rows(
                    payload.rows, stands.get(statistic_id, {})
                )
            else:
                new_rows, rewrites = [], payload.rows
            # The import still carries every rewrite and all the metadata,
            # so the upsert and the relabel are untouched. It runs first
            # for a statistic the recorder has never seen: the bulk task
            # below resolves metadata ids and that row must exist by then.
            if rewrites or meta != state.existing.get(statistic_id):
                imported = True
                async_add_external_statistics(self._hass, payload.metadata, rewrites)
            if new_rows:
                fresh[statistic_id] = (payload.metadata, new_rows)
            # The sum the window reached, which a written row need not
            # carry: an hour whose row already stands with that sum writes
            # nothing, and the next chunk still starts from it.
            next_sums[statistic_id] = payload.ending_sum
            next_existing[statistic_id] = meta

        if fresh:
            imported = True
            # One task for the whole chunk, behind every import it queued.
            get_instance(self._hass).queue_task(BulkInsertTask(fresh))

        return (
            _ChunkState(
                sums=next_sums,
                existing=next_existing,
                # What the next chunk opens in: the last state this one reached.
                carried=transitions[-1][1] if transitions else carried,
            ),
            int((chunk_end - window_start) / HOUR),
            imported,
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
        # A short spell that has not ended by the last row is measured to
        # the end of what was read - or to now, when that is sooner. The
        # decision is provisional then, and the trailing window revisits
        # it once the spell has ended.
        known_until = min(window_end + cfg.min_duration, dt_util.utcnow().timestamp())
        carried, transitions = canonicalise(
            cfg, rows, window_start, window_end, known_until
        )

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

        if transitions and transitions[0][1] == carried:
            # A first row into the state already carried is that state's
            # beginning, not a transition into it: the state machine's
            # `last_changed` IS this row, or an ignored row sat between
            # two spells of it - on the boundary, or a short spell dropped
            # across the seam from the chunk that carried the state.
            # Counting it would count a change from a state to itself.
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
        carried, transitions = canonicalise(
            cfg, rows, window_start, window_end, known_until
        )
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

        The max across every one of the entity's statistics: a statistic
        gets a row only where it has something to record, so any single ID
        can lag the others by any distance. `rows.series_end` answers all
        of them in one read, a seek per statistic.
        """
        if not statistic_ids:
            return None
        return await get_instance(self._hass).async_add_executor_job(
            series_end, self._hass, set(statistic_ids)
        )

    async def _async_base(
        self, statistic_ids: Collection[str], window_start: float
    ) -> tuple[dict[str, float], dict[str, float]]:
        """Return the sums the window continues from, and the previous hour's values.

        One statement, two edges per statistic: the newest row before the
        window - never the newest overall, so a recompute opening inside a
        hole continues from the near side and not from the rows it is
        about to overwrite - and the newest row before the hour before it.
        When the base stands at that hour, the two give it its value by
        difference, which is what `_carried_from_statistics` reads; a row
        rewritten with the carried sum reads as zero there. A statistic
        with no row before the window is absent and starts from zero.
        """
        if not statistic_ids:
            return {}, {}
        previous = window_start - HOUR
        found = await get_instance(self._hass).async_add_executor_job(
            bases, self._hass, set(statistic_ids), (previous, window_start)
        )
        sums: dict[str, float] = {}
        values: dict[str, float] = {}
        for statistic_id, at in found.items():
            newest = at[window_start]
            sums[statistic_id] = newest.sum
            if newest.start == previous:
                before = at.get(previous)
                values[statistic_id] = newest.sum - (
                    0.0 if before is None else before.sum
                )
        return sums, values

    async def async_earliest_recorded_ts(self, entity_id: str) -> float | None:
        """The oldest retained recorder row's timestamp, or None.

        The recorder alone: `None` here means the entity has no history at
        all, which is what a fill asks before deciding which entity ID the
        history is under.
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
        return None

    async def async_earliest_state_ts(self, entity_id: str) -> float | None:
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
        if (recorded := await self.async_earliest_recorded_ts(entity_id)) is not None:
            return recorded

        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        return first_whole_hour(state.last_changed.timestamp())
