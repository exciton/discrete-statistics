"""Read-only recorder queries over our own rows.

The sums are cumulative, and a row stands only where something changed
or once stood, so a value at an edge is the newest row before it - one
index seek on `(metadata_id, start_ts)`. The card reads edges in bulk
through `websocket`; the sensors read one edge at a time here; the
compiler reads its base and the rows standing in its window. All go
through `session_scope(read_only=True)` and none writes - `compiler` is
the only module that does. Edges are answered by `rows_before`, one
statement whatever the number of (statistic, edge) pairs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.components.recorder.statistics import get_metadata_with_session
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session

from .buckets import Row

# Pairs per statement: SQLite caps a compound SELECT at 500 terms. The
# hourly period reads a range instead, so a batch is rarely full.
SEEK_BATCH = 500


def rows_before(
    session: Session, pairs: Iterable[tuple[int, float]]
) -> dict[tuple[int, float], Row]:
    """The newest row strictly before each edge, per (metadata_id, edge) pair.

    One index seek on `(metadata_id, start_ts)` per pair, in one round
    trip, on SQLite, Postgres and MariaDB alike: every pair is its own
    arm of a UNION ALL, so both bounds of its seek are constants -
    MariaDB will not push an outer-referenced bound into a range, and
    plans a correlated form as a walk of the series instead. Cheaper
    per-dialect forms exist for later: a correlated `LIMIT 1` over the
    edges as `json_each` (SQLite) or `jsonb_array_elements` (Postgres).

    A pair with nothing before its edge is absent.
    """
    unique = list(dict.fromkeys(pairs))
    found: dict[tuple[int, float], Row] = {}
    for start in range(0, len(unique), SEEK_BATCH):
        found.update(_seek(session, unique[start : start + SEEK_BATCH]))
    return found


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


def _seek(
    session: Session, batch: Sequence[tuple[int, float]]
) -> dict[tuple[int, float], Row]:
    seeks = union_all(
        *(
            select(
                literal(metadata_id).label("metadata_id"),
                literal(edge).label("edge"),
                _seek_id(metadata_id, edge).label("id"),
            )
            for metadata_id, edge in batch
        )
    ).subquery()
    rows = session.execute(
        select(
            seeks.c.metadata_id, seeks.c.edge, Statistics.start_ts, Statistics.sum
        ).join(Statistics, Statistics.id == seeks.c.id)
    )
    return {
        (metadata_id, edge): Row(start_ts, sum_)
        for metadata_id, edge, start_ts, sum_ in rows
    }


def rows_from(
    session: Session, metadata_ids: Iterable[int], start: float, end: float
) -> dict[int, list[Row]]:
    """Each statistic's newest row before `start`, then its rows in [start, end).

    Ascending, and a statistic with no row either side is absent. One
    statement: the seeks are constant-bound arms as in `rows_before`,
    and the rows inside are one index range - which is what the hourly
    period wants, since there every row answers an edge.
    """
    found: dict[int, list[Row]] = {}
    unique = list(dict.fromkeys(metadata_ids))
    # One term of the compound is the range half; the rest are seeks.
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
    # Ordered here rather than in SQL: a compound select's ORDER BY is
    # dialect-fussy, and the two halves are disjoint and already short.
    for series in found.values():
        series.sort()
    return found


def rows_at(
    session: Session, metadata_ids: set[int], hours: set[float]
) -> dict[int, dict[float, Row]]:
    """Every statistic's rows at the wanted hours, in one query."""
    return _rows(session, metadata_ids, Statistics.start_ts.in_(hours))


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
        return {
            statistic_id: (
                0.0 if (row := found.get((metadata_id, edge))) is None else row.sum
            )
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
