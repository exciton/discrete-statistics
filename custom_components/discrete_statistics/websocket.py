"""The card's query: per-period buckets straight from the rows at the edges.

`recorder/statistics_during_period` reads every hourly row in the range
and reduces them in Python whatever the period is asked for. Our sums are
cumulative and dense, so the card's buckets need only one row per edge:
one `start_ts IN (...)` query for every statistic at once - a range query
when the edges are hours, since then every row is wanted - then a
`LIMIT 1` lookup before any edge that query left blank. The
arithmetic is in `buckets`; this module is the recorder boundary, and it
only reads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.components.recorder.statistics import get_metadata_with_session
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from sqlalchemy import select
from sqlalchemy.orm import Session

from .buckets import Bucket, Period, Row, cut, edges, hours_wanted
from .const import DOMAIN, HOUR

# The most buckets one request may ask for. A chart cannot show more, and
# the edges, the `IN` list and the rows all grow with the count, so a
# range of centuries must be refused rather than walked on the
# recorder's thread.
MAX_BUCKETS = 10_000
# The shortest a period can be, for bounding the count before walking it.
_SHORTEST: dict[Period, float] = {
    "hour": HOUR,
    "day": 23 * HOUR,
    "week": 7 * 23 * HOUR,
    "month": 28 * 24 * HOUR,
    "year": 365 * 24 * HOUR,
}

COMMAND = f"{DOMAIN}/buckets"

BUCKETS_SCHEMA = {
    vol.Required("type"): COMMAND,
    vol.Required("statistic_ids"): vol.All([str], vol.Length(min=1)),
    vol.Required("start_time"): str,
    vol.Required("end_time"): str,
    vol.Required("period"): vol.Any("hour", "day", "week", "month", "year"),
}


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the card's websocket command."""
    websocket_api.async_register_command(hass, ws_buckets)


def _parse(value: str) -> datetime | None:
    if (parsed := dt_util.parse_datetime(value)) is None:
        return None
    return dt_util.as_utc(parsed)


@websocket_api.websocket_command(BUCKETS_SCHEMA)
@websocket_api.async_response
async def ws_buckets(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer the buckets of each statistic between start_time and end_time."""
    start = _parse(msg["start_time"])
    if start is None:
        connection.send_error(msg["id"], "invalid_start_time", "Invalid start_time")
        return
    end = _parse(msg["end_time"])
    if end is None:
        connection.send_error(msg["id"], "invalid_end_time", "Invalid end_time")
        return
    period: Period = msg["period"]
    if end <= start:
        connection.send_error(msg["id"], "invalid_range", "end_time is not after start_time")
        return
    if (end - start).total_seconds() / _SHORTEST[period] > MAX_BUCKETS:
        connection.send_error(
            msg["id"], "range_too_long", f"More than {MAX_BUCKETS} buckets asked for"
        )
        return
    result = await get_instance(hass).async_add_executor_job(
        _buckets,
        hass,
        set(msg["statistic_ids"]),
        start.timestamp(),
        end.timestamp(),
        period,
    )
    connection.send_result(msg["id"], result)


def _buckets(
    hass: HomeAssistant,
    statistic_ids: set[str],
    start: float,
    end: float,
    period: Period,
) -> dict[str, list[dict[str, float]]]:
    """Fetch and cut the buckets. Runs in the executor.

    The edges are aligned in the instance's timezone, as the recorder
    aligns its own, so a chart shows the same days and months whichever
    command drew it.
    """
    edges_ = edges(start, end, period, dt_util.get_default_time_zone())
    with session_scope(hass=hass, read_only=True) as session:
        metadata = get_metadata_with_session(
            get_instance(hass), session, statistic_ids=statistic_ids
        )
        ids = {
            metadata_id: statistic_id
            for statistic_id, (metadata_id, _) in metadata.items()
        }
        # Hourly edges want every row in the range, which a range asks
        # for better than a list of every hour in it.
        at = (
            _rows_between(session, set(ids), edges_[0] - HOUR, edges_[-1])
            if period == "hour"
            else _rows_at(session, set(ids), hours_wanted(edges_))
        )
        return {
            statistic_id: [
                _serialise(b)
                for b in cut(
                    edges_,
                    at.get(metadata_id, {}),
                    lambda edge, m=metadata_id: _newest_before(session, m, edge),
                )
            ]
            for metadata_id, statistic_id in ids.items()
        }


def _serialise(bucket: Bucket) -> dict[str, float]:
    # Milliseconds, as the frontend's own statistics arrive.
    return {
        "start": int(bucket.start * 1000),
        "end": int(bucket.end * 1000),
        "change": bucket.change,
    }


def _rows_at(
    session: Session, metadata_ids: set[int], hours: set[float]
) -> dict[int, dict[float, Row]]:
    """Every statistic's rows at the wanted hours, in one query."""
    return _rows(session, metadata_ids, Statistics.start_ts.in_(hours))


def _rows_between(
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


def _newest_before(session: Session, metadata_id: int, edge: float) -> Row | None:
    row = session.execute(
        select(Statistics.start_ts, Statistics.sum)
        .where(
            Statistics.metadata_id == metadata_id,
            Statistics.sum.is_not(None),
            Statistics.start_ts < edge,
        )
        .order_by(Statistics.start_ts.desc())
        .limit(1)
    ).first()
    return None if row is None else Row(row[0], row[1])
