"""Cut a statistic's cumulative sums into per-period buckets.

The sums are cumulative and dense, so the change over a bucket is the
difference between the rows at its two edges - thirteen rows for a year of
months, not eight thousand hourly ones reduced in Python. This module is
the arithmetic; `websocket` fetches the rows.

Every edge resolves to two rows: the newest at or before it and the oldest
at or after it. With a row on the edge both are that row and adjacent
buckets share it; with a hole straddling the edge the bucket on the left
ends at the last row before the hole and the one on the right starts at
the first row after it, so the hole's time lands in neither. A bucket's
`change` is `sum(left of its end) - sum(left of its start)` and its span is
from its first row to its last, so a ratio of change over span is right
whichever way the edges fell.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, tzinfo
from itertools import pairwise
from typing import Literal, NamedTuple

from .bucketer import hour_start
from .const import HOUR

Period = Literal["hour", "day", "week", "month", "year"]


class Row(NamedTuple):
    """An hourly row: the hour it starts and the sum at its end."""

    start: float
    sum: float


class Bucket(NamedTuple):
    """A finished bucket, spanning its first row to its last."""

    start: float
    end: float
    change: float


def _local(timestamp: float, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(timestamp, tz)


def _floor(timestamp: float, period: Period, tz: tzinfo) -> float:
    """The start of the period containing timestamp.

    Day, week, month and year start at local midnight, weeks on Monday -
    the recorder's own alignment, so a chart's buckets do not move between
    this and `statistics_during_period`.
    """
    if period == "hour":
        return hour_start(timestamp)
    local = _local(timestamp, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        local -= timedelta(days=local.weekday())
    elif period == "month":
        local = local.replace(day=1)
    elif period == "year":
        local = local.replace(month=1, day=1)
    return local.timestamp()


def _next(edge: float, period: Period, tz: tzinfo) -> float:
    """The start of the period after the one starting at edge."""
    if period == "hour":
        return edge + HOUR
    local = _local(edge, tz)
    if period == "day":
        local += timedelta(days=1)
    elif period == "week":
        local += timedelta(days=7)
    elif period == "month":
        # Four days past the 28th is always next month.
        local = (local.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        local = local.replace(year=local.year + 1)
    # Arithmetic on an aware datetime keeps the wall-clock time, which is
    # what a day that changes clocks needs: the next midnight, 23 or 25
    # hours on.
    return local.timestamp()


def edges(start: float, end: float, period: Period, tz: tzinfo) -> list[float]:
    """The edges of every bucket touching [start, end), first to last.

    Snapped outward: the first edge is the start of the period containing
    `start`, the last is the start of the first period at or after `end`.
    A chart drawn from these shows whole periods, as the recorder's does.
    """
    result = [_floor(start, period, tz)]
    while result[-1] < end:
        result.append(_next(result[-1], period, tz))
    return result


Lookup = Callable[[float], Row | None]


def cut(
    edges_: list[float],
    at: Mapping[float, Row],
    newest_before: Lookup,
    oldest_at_or_after: Lookup,
) -> list[Bucket]:
    """Cut the buckets between consecutive edges.

    `at` holds the rows found at the edges' hours - the row starting at an
    edge and the row starting the hour before it - and answers almost
    every edge in one query. The lookups fill in for an edge with no row
    on either side, which is a hole or the start or end of the series,
    and each is asked at most once per hole: a row found for one edge
    answers every edge between it and the next found row.

    A bucket with no row inside it is left out; the card draws that as a
    gap. The sum before a statistic's first row is zero, which is where
    its series began.
    """
    if len(edges_) < 2:
        return []

    def left(edge: float) -> Row | None:
        return at.get(edge - HOUR)

    def right(edge: float) -> Row | None:
        return at.get(edge)

    # Newest row before each edge, walked from the last edge back so that
    # one lookup's answer covers the edges it also precedes.
    lefts: dict[float, Row | None] = {}
    known: Row | None = None
    known_for: float | None = None
    for edge in reversed(edges_):
        row = left(edge)
        if row is None:
            if known_for is None or (known is not None and known.start >= edge):
                known = newest_before(edge)
                known_for = edge
            row = known
        lefts[edge] = row

    # Oldest row at or after each edge but the last, walked forward.
    rights: dict[float, Row | None] = {}
    known = None
    known_for = None
    for edge in edges_[:-1]:
        row = right(edge)
        if row is None:
            if known_for is None or (known is not None and known.start < edge):
                known = oldest_at_or_after(edge)
                known_for = edge
            row = known
        rights[edge] = row

    buckets: list[Bucket] = []
    for a, b in pairwise(edges_):
        first = rights[a]
        last = lefts[b]
        if first is None or last is None or first.start >= b or last.start < a:
            continue
        base = lefts[a]
        buckets.append(
            Bucket(
                start=first.start,
                end=last.start + HOUR,
                change=last.sum - (base.sum if base is not None else 0.0),
            )
        )
    return buckets


def hours_wanted(edges_: list[float]) -> set[float]:
    """The hours whose rows answer the edges: each edge and the hour before."""
    wanted: set[float] = set()
    for edge in edges_:
        wanted.add(edge)
        wanted.add(edge - HOUR)
    return wanted
