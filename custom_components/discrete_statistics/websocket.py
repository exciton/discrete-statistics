"""The card's query: per-period buckets from the rows that answer the edges.

`recorder/statistics_during_period` reads every hourly row in the range
and reduces them in Python whatever the period is asked for. Our sums
are cumulative, so a bucket needs only the newest row before each of its
edges, which `rows.edge_rows` answers in one statement. The entity's
duration statistics ride along so a bucket can be told compiled from a
hole. The arithmetic is in `buckets` and the queries in `rows`; this
module only joins the two.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .buckets import (
    Bucket,
    Period,
    before_edges,
    cut,
    edges,
    family_of,
    has_row,
    judges,
)
from .const import DOMAIN, HOUR
from .rows import edge_rows, metadata_ids
from .statistic_ids import family

# The most buckets one request may ask for. A chart cannot show more, and
# the edges and the rows grow with the count - as do the statement's arms
# on MySQL/MariaDB - so a range of centuries must be refused rather than
# walked on the recorder's thread.
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
    command drew it. The statistics that judge gap from zero ride along
    in the read, so a chart of one rare state does not show a gap in
    every period it did not occur.
    """
    edges_ = edges(start, end, period, dt_util.get_default_time_zone())
    with session_scope(hass=hass, read_only=True) as session:
        slugs = {slug for sid in statistic_ids if (slug := family(sid)) is not None}
        ours = metadata_ids(session, statistic_ids, slugs)
        requested = {sid for sid in statistic_ids if sid in ours}
        if not requested:
            return {}
        judged_by = judges(ours, requested)
        wanted = requested.union(*judged_by.values())
        ids = {sid: ours[sid] for sid in wanted}
        found = edge_rows(session, ids.values(), edges_, hourly=period == "hour")
        before = {
            sid: before_edges(found.get(mid, ()), edges_) for sid, mid in ids.items()
        }

        def compiled_by(slug: str):
            judged = [before[sid] for sid in judged_by[slug]]
            return lambda a, b: any(has_row(rows, a, b) for rows in judged)

        return {
            sid: [
                _serialise(b)
                for b in cut(edges_, before[sid], compiled_by(family_of(sid)))
            ]
            for sid in requested
        }


def _serialise(bucket: Bucket) -> dict[str, float]:
    # Milliseconds, as the frontend's own statistics arrive.
    return {
        "start": int(bucket.start * 1000),
        "end": int(bucket.end * 1000),
        "change": bucket.change,
    }
