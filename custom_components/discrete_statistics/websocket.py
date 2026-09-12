"""The card's query: per-period buckets from the rows that answer the edges.

`recorder/statistics_during_period` reads every hourly row in the range
and reduces them in Python whatever the period is asked for. Our sums
are cumulative, so a bucket needs only the newest row before each of its
edges, and one statement answers every (statistic, edge) pair at once -
`rows.rows_before`, or `rows.rows_from` at the hourly period, where
every row in the range answers an edge anyway. The entity's duration
statistics ride along so a bucket can be told compiled from a hole. The
arithmetic is in `buckets` and the queries in `rows`; this module only
reads.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata_with_session
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from sqlalchemy.orm import Session

from .buckets import Bucket, Period, Row, cut, edges, has_row
from .const import DOMAIN, HOUR, METRIC_DURATION
from .rows import rows_before, rows_from
from .statistic_ids import parse

# The most buckets one request may ask for. A chart cannot show more, and
# the edges, the statement's arms and the rows all grow with the count,
# so a range of centuries must be refused rather than walked on the
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
        connection.send_error(
            msg["id"], "invalid_range", "end_time is not after start_time"
        )
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


def _family(statistic_id: str) -> str:
    """The entity a statistic belongs to, as its ID names it."""
    parts = parse(statistic_id)
    return statistic_id if parts is None else parts[0]


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
    command drew it. Gap or zero is judged on each entity's duration
    statistics as a whole, which ride along in every read: a chart of one
    rare state must not show a gap in every period it did not occur.
    """
    edges_ = edges(start, end, period, dt_util.get_default_time_zone())
    with session_scope(hass=hass, read_only=True) as session:
        instance = get_instance(hass)
        ours = get_metadata_with_session(instance, session, statistic_source=DOMAIN)
        requested = {sid for sid in statistic_ids if sid in ours}
        if not requested:
            return {}
        durations: dict[str, set[str]] = {}
        for sid in ours:
            parts = parse(sid)
            if parts is not None and parts[2] == METRIC_DURATION:
                durations.setdefault(parts[0], set()).add(sid)
        judges: dict[str, set[str]] = {}
        for statistic_id in requested:
            family = _family(statistic_id)
            judges.setdefault(family, set()).update(durations.get(family, ()))
        # An entity with no duration statistic left is judged on what was
        # asked of it: every requested ID of that entity, not just one.
        for family, judged in judges.items():
            if not judged:
                judges[family] = {sid for sid in requested if _family(sid) == family}
        wanted = requested.union(*judges.values())
        ids = {sid: ours[sid][0] for sid in wanted}

        if period == "hour":
            before = _hourly(session, ids, edges_)
        else:
            found = rows_before(
                session, [(mid, edge) for mid in ids.values() for edge in edges_]
            )
            before = {
                sid: {edge: found.get((mid, edge)) for edge in edges_}
                for sid, mid in ids.items()
            }

        def compiled_by(family: str):
            judged = [before[sid] for sid in judges[family]]
            return lambda a, b: any(has_row(rows, a, b) for rows in judged)

        return {
            sid: [
                _serialise(b)
                for b in cut(edges_, before[sid], compiled_by(_family(sid)))
            ]
            for sid in requested
        }


def _hourly(
    session: Session, ids: dict[str, int], edges_: list[float]
) -> dict[str, dict[float, Row | None]]:
    """Every edge's row, from the one read the hourly period wants.

    Each statistic's rows in the range answer every edge but the first,
    which the row the read opens on answers.
    """
    found = rows_from(session, set(ids.values()), edges_[0], edges_[-1])
    before: dict[str, dict[float, Row | None]] = {}
    for sid, metadata_id in ids.items():
        series = found.get(metadata_id, [])
        starts = [row.start for row in series]
        before[sid] = {
            edge: (series[at - 1] if (at := bisect_left(starts, edge)) else None)
            for edge in edges_
        }
    return before


def _serialise(bucket: Bucket) -> dict[str, float]:
    # Milliseconds, as the frontend's own statistics arrive.
    return {
        "start": int(bucket.start * 1000),
        "end": int(bucket.end * 1000),
        "change": bucket.change,
    }
