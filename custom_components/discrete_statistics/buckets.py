"""Cut a statistic's cumulative sums into per-period buckets.

The sums are cumulative, so the change over a bucket is the difference
between the rows at its two edges - thirteen rows for a year of months,
not eight thousand hourly ones reduced in Python. This module is
the arithmetic; `websocket` fetches the rows, in one statement.

Every edge resolves to the newest row before it, whose sum is the sum at
the edge, and adjacent buckets share it: a bucket's `change` is `sum(left
of its end) - sum(left of its start)`, over the whole period between the
edges. The rows are sparse, so an edge may resolve to a row hours or
months behind it; that resolution is the read's, and this module only
cuts. A statistic has no time in its state before its series begins, and a
hole is time in no state, so the period's length is the right thing for
a ratio to divide by whichever of those falls inside it.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
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
    """A finished bucket: its period's edges and the change between them.

    The edges are the same for every statistic cut on them, so a chart
    stacks the statistics on one bar.
    """

    start: float
    end: float
    change: float


def _local(timestamp: float, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(timestamp, tz)


def floor(timestamp: float, period: Period, tz: tzinfo) -> float:
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


def after(edge: float, period: Period, tz: tzinfo) -> float:
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
    result = [floor(start, period, tz)]
    while result[-1] < end:
        result.append(after(result[-1], period, tz))
    return result


def before_edges(
    series: Sequence[Row], edges_: Sequence[float]
) -> dict[float, Row | None]:
    """Each edge's row: the newest in `series` that starts before it.

    `series` is ascending, and needs to hold only the distinct answers -
    a row left out because it was never the newest before any edge is
    never the answer here either, since a row between an edge and its
    answer would itself be the newer one. The read hands back exactly
    that set, whether it seeks per edge (`rows.rows_before`) or reads a
    range (`rows.rows_from`), which is why both resolve here.
    """
    starts = [row.start for row in series]
    return {
        edge: (series[at - 1] if (at := bisect_left(starts, edge)) else None)
        for edge in edges_
    }


def has_row(before: Mapping[float, Row | None], start: float, end: float) -> bool:
    """Whether a row stands inside [start, end)."""
    row = before[end]
    return row is not None and row.start >= start


def cut(
    edges_: list[float],
    before: Mapping[float, Row | None],
    compiled: Callable[[float, float], bool],
) -> list[Bucket]:
    """Cut the buckets between consecutive edges, from the row before each.

    `compiled` says whether the entity was compiled inside a bucket - the
    caller judges that on the entity's duration rows as a whole - so a
    statistic with no row of its own in a compiled bucket reads zero, and
    only a hole is left out for the card to draw as a gap. The sum before
    a statistic's first row is zero, which is where its series began.
    """
    buckets: list[Bucket] = []
    for a, b in pairwise(edges_):
        if not compiled(a, b):
            continue
        last, base = before[b], before[a]
        buckets.append(
            Bucket(
                start=a,
                end=b,
                change=(last.sum if last else 0.0) - (base.sum if base else 0.0),
            )
        )
    return buckets
