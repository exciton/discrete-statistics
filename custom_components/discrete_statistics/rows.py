"""Read-only recorder queries over our own rows.

The sums are cumulative, and a row stands only where something changed
or once stood, so a value at an edge is the newest row before it - one
index seek on `(metadata_id, start_ts)`. The card reads its edges in
bulk through `websocket` and the sensors theirs through `sums_at_edges`,
both on the one shape; the compiler reads its base and the rows standing
in its window. All go through `session_scope(read_only=True)` and none
writes - `compiler` is the only module that does. Edges are answered by
`rows_before`, one statement whatever the number of (statistic, edge)
pairs - except on MySQL/MariaDB and an engine we do not know, where the
pairs batch at `SEEK_BATCH`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsMeta
from homeassistant.components.recorder.statistics import get_metadata_with_session
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from sqlalchemy import or_, select, text, union_all
from sqlalchemy.orm import Session

from .buckets import Row, before_edges
from .const import DOMAIN
from .statistic_ids import parse

# Arms per statement: SQLite caps a compound SELECT at 500 terms, and only
# the arm rendering is compound. The hourly period reads a range instead,
# so a batch is rarely full.
SEEK_BATCH = 500

# The engines that expand the pair list in SQL, and the expression that
# reads a pair out of it. One statement whatever the number of pairs, and
# one plan: the seek is planned once and run per pair.
_PAIRS = {
    "sqlite": (
        "json_each(:pairs)",
        "json_extract(value, '$[0]')",
        "json_extract(value, '$[1]')",
    ),
    # Nothing in CI parses this one: its syntax is verified by hand
    # against a real server, through the EXPLAIN in the branch's report.
    "postgresql": (
        "jsonb_array_elements(CAST(:pairs AS jsonb)) AS element(value)",
        "(value->>0)::integer",
        "(value->>1)::double precision",
    ),
}


# The character that escapes a LIKE wildcard in `metadata_ids`. A slug is
# `[a-z0-9_]`, so anything outside it will do and nothing needs escaping
# but the underscore itself.
_ESCAPE = "/"


def metadata_ids(
    session: Session, statistic_ids: Iterable[str], entity_slugs: Iterable[str]
) -> dict[str, int]:
    """Our statistics' metadata ids: those named, and those of those entities.

    The siblings are what tells a compiled bucket from a hole, and asking
    for them by entity keeps the read the size of the chart:
    `get_metadata_with_session(statistic_source=DOMAIN)` answers with every
    statistic we hold, which grows with the install and not with the
    request - six rows against a hundred here, and further apart the more
    entities are configured.

    `LIKE` is the prefix filter, and `_` is one of its wildcards, so the
    slug is escaped; a longer slug sharing the prefix still matches, and
    is dropped in Python against the ID grammar, which is the only place
    that boundary is exact.
    """
    named = set(statistic_ids)
    slugs = set(entity_slugs)
    if not named and not slugs:
        return {}
    prefixes = [
        StatisticsMeta.statistic_id.like(
            f"{DOMAIN}:{slug.replace(_ESCAPE, _ESCAPE * 2).replace('_', _ESCAPE + '_')}"
            f"{_ESCAPE}_%",
            escape=_ESCAPE,
        )
        for slug in slugs
    ]
    found = session.execute(
        select(StatisticsMeta.statistic_id, StatisticsMeta.id).where(
            StatisticsMeta.source == DOMAIN,
            or_(StatisticsMeta.statistic_id.in_(named), *prefixes),
        )
    )
    return {
        statistic_id: metadata_id
        for statistic_id, metadata_id in found
        if statistic_id in named
        or ((parts := parse(statistic_id)) is not None and parts[0] in slugs)
    }


def _pair_seek(dialect: str) -> Any:
    """The whole seek as one correlated statement over an expanded pair list.

    The subquery picks the newest row before the pair's edge; the join
    fetches the distinct ones. No `id IS NOT NULL` filter - the inner
    join drops the misses, and the filter has Postgres evaluate the
    subplan twice. The edge is not returned: a row answers every edge it
    is the newest before, so one copy of it is the whole answer.
    """
    source, metadata_id, edge = _PAIRS[dialect]
    return text(
        f"""
        SELECT statistics.metadata_id, statistics.start_ts, statistics.sum
        FROM (
            SELECT DISTINCT (
                SELECT newest.id FROM statistics AS newest
                WHERE newest.metadata_id = pair.metadata_id
                  AND newest.start_ts < pair.edge
                  AND newest.sum IS NOT NULL
                ORDER BY newest.start_ts DESC
                LIMIT 1
            ) AS id
            FROM (
                SELECT {metadata_id} AS metadata_id, {edge} AS edge
                FROM {source}
            ) AS pair
        ) AS picked
        JOIN statistics ON statistics.id = picked.id
        """
    )


def seek_statements(
    dialect: str | None, pairs: Sequence[tuple[int, float]]
) -> list[Any]:
    """The statements a dialect runs to answer every (metadata_id, edge) pair.

    Each yields the distinct picked rows, as `(metadata_id, start_ts,
    sum)`. One statement on SQLite and Postgres; one per `SEEK_BATCH`
    arms elsewhere.
    """
    if dialect in _PAIRS:
        return [_pair_seek(dialect).bindparams(pairs=json.dumps(pairs))]
    return [
        _arms(pairs[at : at + SEEK_BATCH]) for at in range(0, len(pairs), SEEK_BATCH)
    ]


def rows_before(
    session: Session, pairs: Iterable[tuple[int, float]]
) -> dict[int, list[Row]]:
    """The rows answering the pairs, per statistic, ascending and distinct.

    One index seek on `(metadata_id, start_ts)` per pair, and the shape
    of the statement is the engine's: SQLite expands the pairs with
    `json_each` and Postgres with `jsonb_array_elements`, both a single
    correlated seek; MySQL/MariaDB and an engine we do not know get one
    constant-bound arm per pair, batched at `SEEK_BATCH`. Postgres plans
    every arm separately, so expanding pays there - measured on a
    13.9 GB database, 2,408 pairs: arms 9 ms SQLite / 163 ms Postgres,
    expanded 10 ms SQLite / 15 ms Postgres. MySQL/MariaDB keeps the arms
    because MariaDB will not push an outer-referenced bound into a
    range: with the pairs handed in through `JSON_TABLE` it plans the
    correlated seek as `ref` on `metadata_id` alone and walks the series
    per pair, 477 ms against the arms' 35 on 472 pairs of the sparse
    bench database.

    The rows come back distinct, without the edges that picked them, and
    a caller resolves an edge by `buckets.before_edges`: the newest
    returned row before an edge is that edge's answer, because a row
    between the two would itself have been picked by it. So a rare state
    whose one row answers thirteen edges is one row, not thirteen.

    The engine comes from the session's bind, so no caller threads it
    through and the read stays a session away from `hass`.

    A statistic with nothing before any of its edges is absent.
    """
    unique = list(dict.fromkeys(pairs))
    if not unique:
        return {}
    # Distinct across the batches too: the arms only dedupe within one.
    found: dict[int, set[Row]] = {}
    for statement in seek_statements(session.get_bind().dialect.name, unique):
        for metadata_id, start_ts, sum_ in session.execute(statement):
            found.setdefault(metadata_id, set()).add(Row(start_ts, sum_))
    return {metadata_id: sorted(rows) for metadata_id, rows in found.items()}


def _seek(
    column: Any,
    metadata_id: int,
    edge: float | None = None,
    *,
    descending: bool | None = None,
) -> Any:
    """One row of one statistic, as a constant-bound seek on `(metadata_id, start_ts)`.

    The newest row before an edge, or - with no edge - the oldest or the
    newest row the statistic holds, which is the same index walked
    either way. An edge is descending unless told otherwise.
    """
    if descending is None:
        descending = edge is not None
    return (
        select(column)
        .where(
            Statistics.metadata_id == metadata_id,
            Statistics.sum.is_not(None),
            *(() if edge is None else (Statistics.start_ts < edge,)),
        )
        .order_by(
            Statistics.start_ts.desc() if descending else Statistics.start_ts.asc()
        )
        .limit(1)
        # The arm carries its own FROM: correlating it against the outer
        # join would drop the bounds this is built for.
        .correlate(None)
        .scalar_subquery()
    )


def _seek_id(metadata_id: int, edge: float) -> Any:
    """The id of the newest row before an edge, as a constant-bound seek."""
    return _seek(Statistics.id, metadata_id, edge)


def _arms(batch: Sequence[tuple[int, float]]) -> Any:
    """One constant-bound seek per pair, unioned: the portable rendering.

    Distinct before the join, so a row several edges share is fetched
    once - which is also what makes the pairs' own columns unnecessary.

    Written out rather than built from the Core, and with the bounds
    literal rather than bound: five hundred arms are five hundred
    subqueries to construct and compile, and that Python costs more
    than the server spends answering them - 472 pairs on MariaDB, 151 ms
    through the Core against 35 ms as text, where the server's own share
    of either is about 40. The bounds are coerced at the format site:
    `int()` and `float()` can yield nothing but a number, so the
    rendering is not an injection, and `repr` round-trips the timestamp
    exactly.
    """
    arms = " UNION ALL ".join(
        "SELECT (SELECT id FROM statistics"
        f" WHERE metadata_id = {int(metadata_id)}"
        f" AND sum IS NOT NULL AND start_ts < {float(edge)!r}"
        " ORDER BY start_ts DESC LIMIT 1) AS id"
        for metadata_id, edge in batch
    )
    return text(
        "SELECT statistics.metadata_id, statistics.start_ts, statistics.sum"
        f" FROM (SELECT DISTINCT arm.id AS id FROM ({arms}) AS arm) AS picked"
        " JOIN statistics ON statistics.id = picked.id"
    )


def rows_from(
    session: Session, metadata_ids: Iterable[int], start: float, end: float
) -> dict[int, list[Row]]:
    """Each statistic's newest row before `start`, then its rows in [start, end).

    Ascending, and a statistic with no row either side is absent. One
    statement: the seeks are constant-bound arms as in `rows_before`,
    and the rows inside are one index range - which is what the hourly
    period wants, since there every row answers an edge. The arms stay
    on every engine here: there is one per statistic, not one per pair,
    so a per-dialect form would save nothing.
    """
    found: dict[int, list[Row]] = {}
    unique = list(dict.fromkeys(metadata_ids))
    # A margin of one, so the arms stay short of the cap whatever the
    # range half compiles to.
    for at in range(0, len(unique), SEEK_BATCH - 1):
        found.update(_from(session, unique[at : at + SEEK_BATCH - 1], start, end))
    return found


def _from(
    session: Session, batch: Sequence[int], start: float, end: float
) -> dict[int, list[Row]]:
    seeks = union_all(
        *(select(_seek_id(metadata_id, start).label("id")) for metadata_id in batch)
    ).subquery()
    columns = (Statistics.metadata_id, Statistics.start_ts, Statistics.sum)
    rows = session.execute(
        union_all(
            select(*columns).join(seeks, Statistics.id == seeks.c.id),
            select(*columns).where(
                Statistics.metadata_id.in_(batch),
                Statistics.sum.is_not(None),
                Statistics.start_ts >= start,
                Statistics.start_ts < end,
            ),
        )
    )
    found: dict[int, list[Row]] = {}
    for metadata_id, start_ts, sum_ in rows:
        found.setdefault(metadata_id, []).append(Row(start_ts, sum_))
    # Ordered here rather than in SQL: an ORDER BY on the compound
    # filesorts the whole union on MySQL, range rows included - the cost
    # this read exists to avoid.
    for series in found.values():
        series.sort()
    return found


def rows_between(
    session: Session, metadata_ids: set[int], start: float, end: float
) -> dict[int, dict[float, Row]]:
    """Every statistic's rows in [start, end), in one query."""
    return _rows(
        session,
        metadata_ids,
        Statistics.start_ts >= start,
        Statistics.start_ts < end,
    )


def _rows(
    session: Session, metadata_ids: set[int], *where: Any
) -> dict[int, dict[float, Row]]:
    rows = session.execute(
        select(Statistics.metadata_id, Statistics.start_ts, Statistics.sum).where(
            Statistics.metadata_id.in_(metadata_ids),
            Statistics.sum.is_not(None),
            *where,
        )
    )
    result: dict[int, dict[float, Row]] = {}
    for metadata_id, start_ts, sum_ in rows:
        result.setdefault(metadata_id, {})[start_ts] = Row(start_ts, sum_)
    return result


def newest_before(
    session: Session, metadata_id: int, edge: float, limit: int = 1
) -> list[Row]:
    """Up to `limit` rows before the edge, newest first. One seek whatever the limit."""
    rows = session.execute(
        select(Statistics.start_ts, Statistics.sum)
        .where(
            Statistics.metadata_id == metadata_id,
            Statistics.sum.is_not(None),
            Statistics.start_ts < edge,
        )
        .order_by(Statistics.start_ts.desc())
        .limit(limit)
    )
    return [Row(start_ts, sum_) for start_ts, sum_ in rows]


@contextmanager
def _ids(
    hass: HomeAssistant, statistic_ids: Iterable[str]
) -> Iterator[tuple[Session, dict[int, str]]]:
    """A read-only session and the metadata ids of ours among those asked for.

    Keyed by metadata id, which is what a row carries back; a statistic
    the recorder does not hold is simply absent.
    """
    with session_scope(hass=hass, read_only=True) as session:
        metadata = get_metadata_with_session(
            get_instance(hass), session, statistic_ids=statistic_ids
        )
        yield (
            session,
            {
                metadata_id: statistic_id
                for statistic_id, (metadata_id, _) in metadata.items()
            },
        )


def bases(
    hass: HomeAssistant, statistic_ids: set[str], edge: float
) -> dict[str, list[Row]]:
    """The two newest rows of each statistic before an edge. Executor.

    The newest is the sum a window continues from; the pair gives the
    hour before the edge its value by difference. A statistic with no
    row before the edge is absent.
    """
    with _ids(hass, statistic_ids) as (session, ids):
        found = {
            statistic_id: newest_before(session, metadata_id, edge, 2)
            for metadata_id, statistic_id in ids.items()
        }
        return {statistic_id: rows for statistic_id, rows in found.items() if rows}


def standing(
    hass: HomeAssistant, statistic_ids: set[str], start: float, end: float
) -> dict[str, set[float]]:
    """The hours in [start, end) at which each statistic already holds a row. Executor."""
    with _ids(hass, statistic_ids) as (session, ids):
        between = rows_between(session, set(ids), start, end)
        return {ids[metadata_id]: set(rows) for metadata_id, rows in between.items()}


def sums_at_edges(
    hass: HomeAssistant, statistic_ids: set[str], edges: set[float]
) -> dict[float, dict[str, float]]:
    """Each statistic's cumulative sum at each edge. Runs in the executor.

    One statement covering every (statistic, edge) pair, resolved per edge by
    `before_edges` - the same read the card makes, and the reason a
    refresh's several edges cost one statement rather than one each.

    Zero before a statistic's first row - a state has no time in it before
    its series begins - and absent for a statistic the recorder does not
    hold, which is how a caller tells "not yet" from "never".
    """
    wanted = sorted(edges)
    with _ids(hass, statistic_ids) as (session, ids):
        found = rows_before(
            session,
            [(metadata_id, edge) for metadata_id in ids for edge in wanted],
        )
        resolved = {
            metadata_id: before_edges(found.get(metadata_id, ()), wanted)
            for metadata_id in ids
        }
        return {
            edge: {
                # A row is a two-tuple, so it is truthy whatever its sum.
                statistic_id: (row.sum if (row := resolved[metadata_id][edge]) else 0.0)
                for metadata_id, statistic_id in ids.items()
            }
            for edge in wanted
        }


def _earliest(batch: Sequence[int]) -> Any:
    """One arm per statistic, each the start of its oldest row."""
    return union_all(
        *(
            select(_seek(Statistics.start_ts, metadata_id).label("start_ts"))
            for metadata_id in batch
        )
    )


def _latest(batch: Sequence[int]) -> Any:
    """One arm per statistic, each the start of its newest row."""
    return union_all(
        *(
            select(
                _seek(Statistics.start_ts, metadata_id, descending=True).label(
                    "start_ts"
                )
            )
            for metadata_id in batch
        )
    )


def _bound(
    hass: HomeAssistant, statistic_ids: set[str], arms: Any, pick: Any
) -> float | None:
    """One seek per statistic, unioned, and the bound taken in Python.

    An aggregate over the set reads as a scan to Postgres, which walks
    `ix_statistics_start_ts` from one end of the table filtering for
    ours - 4.6 M rows and over a second where the seeks are two index
    lookups. A statistic with no row contributes nothing.
    """
    with _ids(hass, statistic_ids) as (session, found_ids):
        ids = list(found_ids)
        found = [
            start_ts
            for at in range(0, len(ids), SEEK_BATCH)
            for (start_ts,) in session.execute(arms(ids[at : at + SEEK_BATCH]))
            if start_ts is not None
        ]
        return pick(found, default=None)


def series_start(hass: HomeAssistant, statistic_ids: set[str]) -> float | None:
    """The start of the earliest row across the statistics, or None. Executor."""
    return _bound(hass, statistic_ids, _earliest, min)


def series_end(hass: HomeAssistant, statistic_ids: set[str]) -> float | None:
    """The start of the newest row across the statistics, or None. Executor.

    The watermark: the rows are dense only from a state's first
    appearance, so the newest row of any one statistic can lag the
    others by any distance.
    """
    return _bound(hass, statistic_ids, _latest, max)
