"""Read-only recorder queries over our own rows.

The sums are cumulative, and a row stands only where something changed
or once stood, so a value at an edge is the newest row before it - one
index seek on `(metadata_id, start_ts)`. The card reads edges in bulk
through `websocket`; the sensors read one edge at a time here; the
compiler reads its base and the rows standing in its window. All go
through `session_scope(read_only=True)` and none writes - `compiler` is
the only module that does.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.components.recorder.statistics import get_metadata_with_session
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .buckets import Row, row_before


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
        at = rows_at(session, set(ids), {row_before(edge)})
        result: dict[str, float] = {}
        for metadata_id, statistic_id in ids.items():
            row = at.get(metadata_id, {}).get(row_before(edge))
            if row is None:
                found = newest_before(session, metadata_id, edge)
                row = found[0] if found else None
            result[statistic_id] = 0.0 if row is None else row.sum
        return result


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
