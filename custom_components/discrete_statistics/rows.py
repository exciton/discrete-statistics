"""Read-only recorder queries over our own rows.

The sums are cumulative, and a row stands only where something changed
or once stood, so a value at an edge is the newest row before it - one
index seek on `(metadata_id, start_ts)`. The card reads edges in bulk
through `websocket`; the sensors read one edge at a time here; the
compiler reads its base and the rows standing in its window. All go
through `session_scope(read_only=True)` and none writes - `compiler` is
the only module that does. Edges are answered by `rows_before`, one
statement whatever the number of (statistic, edge) pairs - except on
MySQL/MariaDB and an engine we do not know, where the pairs batch at
`SEEK_BATCH`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsMeta
from homeassistant.components.recorder.statistics import get_metadata_with_session
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from sqlalchemy import func, or_, select, text, union_all
from sqlalchemy.orm import Session

from .buckets import Row
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
    constant-bound arm per pair, batched at `SEEK_BATCH`. MariaDB will
    not push an outer-referenced bound into a range and walks the series
    instead, and Postgres plans every arm separately - measured on a
    13.9 GB database, 2,408 pairs: arms 9 ms SQLite / 108 ms MariaDB /
    163 ms Postgres, expanded 10 ms SQLite / 15 ms Postgres.

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


def _seek_id(metadata_id: int, edge: float) -> Any:
    """The id of the newest row before an edge, as a constant-bound seek."""
    return (
        select(Statistics.id)
        .where(
            Statistics.metadata_id == metadata_id,
            Statistics.sum.is_not(None),
            Statistics.start_ts < edge,
        )
        .order_by(Statistics.start_ts.desc())
        .limit(1)
        # The arm carries its own FROM: correlating it against the outer
        # join would drop the bounds this is built for.
        .correlate(None)
        .scalar_subquery()
    )


def _arms(batch: Sequence[tuple[int, float]]) -> Any:
    """One constant-bound seek per pair, unioned: the portable rendering.

    Distinct before the join, so a row several edges share is fetched
    once - which is also what makes the pairs' own columns unnecessary.
    """
    picked = union_all(
        *(
            select(_seek_id(metadata_id, edge).label("id"))
            for metadata_id, edge in batch
        )
    ).subquery()
    ids = select(picked.c.id).distinct().subquery()
    return select(Statistics.metadata_id, Statistics.start_ts, Statistics.sum).join(
        ids, Statistics.id == ids.c.id
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


def bases(
    hass: HomeAssistant, statistic_ids: set[str], edge: float
) -> dict[str, list[Row]]:
    """The two newest rows of each statistic before an edge. Executor.

    The newest is the sum a window continues from; the pair gives the
    hour before the edge its value by difference. A statistic with no
    row before the edge is absent.
    """
    with session_scope(hass=hass, read_only=True) as session:
        metadata = get_metadata_with_session(
            get_instance(hass), session, statistic_ids=statistic_ids
        )
        found = {
            statistic_id: newest_before(session, metadata_id, edge, 2)
            for statistic_id, (metadata_id, _) in metadata.items()
        }
        return {statistic_id: rows for statistic_id, rows in found.items() if rows}


def standing(
    hass: HomeAssistant, statistic_ids: set[str], start: float, end: float
) -> dict[str, set[float]]:
    """The hours in [start, end) at which each statistic already holds a row. Executor."""
    with session_scope(hass=hass, read_only=True) as session:
        metadata = get_metadata_with_session(
            get_instance(hass), session, statistic_ids=statistic_ids
        )
        ids = {
            metadata_id: statistic_id
            for statistic_id, (metadata_id, _) in metadata.items()
        }
        between = rows_between(session, set(ids), start, end)
        return {ids[metadata_id]: set(rows) for metadata_id, rows in between.items()}


def sums_at(
    hass: HomeAssistant, statistic_ids: set[str], edge: float
) -> dict[str, float]:
    """The cumulative sum of each statistic at an edge. Runs in the executor.

    Zero before a statistic's first row - a state has no time in it before
    its series begins - and absent for a statistic the recorder does not
    hold, which is how a caller tells "not yet" from "never".
    """
    with session_scope(hass=hass, read_only=True) as session:
        metadata = get_metadata_with_session(
            get_instance(hass), session, statistic_ids=statistic_ids
        )
        ids = {
            metadata_id: statistic_id
            for statistic_id, (metadata_id, _) in metadata.items()
        }
        found = rows_before(session, [(metadata_id, edge) for metadata_id in ids])
        # One edge per statistic, so the newest row returned for it is
        # that edge's answer.
        return {
            statistic_id: (rows[-1].sum if (rows := found.get(metadata_id)) else 0.0)
            for metadata_id, statistic_id in ids.items()
        }


def series_start(hass: HomeAssistant, statistic_ids: set[str]) -> float | None:
    """The start of the earliest row across the statistics, or None. Executor."""
    with session_scope(hass=hass, read_only=True) as session:
        metadata = get_metadata_with_session(
            get_instance(hass), session, statistic_ids=statistic_ids
        )
        ids = {metadata_id for metadata_id, _ in metadata.values()}
        if not ids:
            return None
        return session.execute(
            select(func.min(Statistics.start_ts)).where(
                Statistics.metadata_id.in_(ids), Statistics.sum.is_not(None)
            )
        ).scalar()
