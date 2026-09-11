"""Cut a statistic's cumulative sums into per-period buckets.

The sums are cumulative and dense, so the change over a bucket is the
difference between the rows at its two edges - thirteen rows for a year of
months, not eight thousand hourly ones reduced in Python. This module is
the arithmetic; `websocket` fetches the rows.

Every edge resolves to the newest row before it, whose sum is the sum at
the edge, and adjacent buckets share it: a bucket's `change` is `sum(left
of its end) - sum(left of its start)`, over the whole period between the
edges. The rows are sparse, so which reads settle which edges is tracked
in `Known`; `websocket` drives the reads and this module only resolves.
A statistic has no time in its state before its series begins, and a
hole is time in no state, so the period's length is the right thing for
a ratio to divide by whichever of those falls inside it.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Iterable
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


def row_before(edge: float) -> float:
    """The start of the newest hour that ends at or before edge.

    The hour before it when the edge is on the hour; otherwise the hour
    containing it, since a zone half an hour off UTC puts every edge at
    half past and the row holding the sum at the edge is the one running
    through it - the row the recorder's own reduction puts in that day.
    """
    return (math.ceil(edge / HOUR) - 1) * HOUR


# Rows fetched per seek: one index seek whatever the count, and enough
# that a rare state's whole series usually comes back in one.
LOOKUP_ROWS = 16
# How many rows a seek is worth. A seek is a round trip, a row a few
# microseconds of Python; below this many rows per unsettled edge, one
# range read beats the seeks it saves.
SEEK_WORTH = 8


class Known:
    """One statistic's rows as found so far, and which edges they settle.

    An edge is settled when the newest row before it is certainly held: a
    row at `row_before(edge)` settles that edge alone; a run taken at the
    newest unsettled edge settles every edge down to the run's oldest row,
    and every edge at all when the run came back short; a range read
    settles every edge down to its oldest row.
    """

    def __init__(self, rows: Iterable[Row] = ()) -> None:
        self._rows: dict[float, Row] = {}
        self._starts: list[float] = []
        self.floor = math.inf
        self.add(rows)

    def add(self, rows: Iterable[Row]) -> None:
        for row in rows:
            if row.start not in self._rows:
                bisect.insort(self._starts, row.start)
            self._rows[row.start] = row

    def seek(self, rows: list[Row]) -> None:
        """Take a newest-first run fetched at the newest unsettled edge."""
        self.add(rows)
        exhausted = len(rows) < LOOKUP_ROWS
        self.floor = min(self.floor, -math.inf if exhausted else rows[-1].start)

    def ranged(self, rows: Iterable[Row]) -> None:
        """Take every row of a span that starts at or below the newest unsettled edge."""
        rows = list(rows)
        self.add(rows)
        if rows:
            self.floor = min(self.floor, min(row.start for row in rows))

    def settled(self, edge: float) -> bool:
        return edge > self.floor or row_before(edge) in self._rows

    def before(self, edge: float) -> Row | None:
        """The newest held row before the edge."""
        index = bisect.bisect_left(self._starts, edge)
        return self._rows[self._starts[index - 1]] if index else None


def blanks(known: Known, edges_: list[float]) -> list[float]:
    """The edges not yet settled, newest first."""
    return [edge for edge in reversed(edges_) if not known.settled(edge)]


def wants_range(
    known: Known, edges_: list[float], run: list[Row]
) -> tuple[float, float] | None:
    """The span to read whole instead of seeking on, or None.

    A full run's density says how many rows the unsettled span holds;
    under `SEEK_WORTH` per unsettled edge, one range read is the cheaper
    way to settle them.
    """
    if len(run) < LOOKUP_ROWS:
        return None
    blank = blanks(known, edges_)
    if not blank:
        return None
    lo, hi = row_before(blank[-1]), row_before(blank[0]) + HOUR
    density = LOOKUP_ROWS / (run[0].start - run[-1].start + HOUR)
    if density * (hi - lo) < SEEK_WORTH * len(blank):
        return lo, hi
    return None


def has_row(known: Known, start: float, end: float) -> bool:
    row = known.before(end)
    return row is not None and row.start >= start


def cut(
    edges_: list[float],
    known: Known,
    compiled: Callable[[float, float], bool],
) -> list[Bucket]:
    """Cut the buckets between consecutive edges. Every edge must be settled.

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
        last, base = known.before(b), known.before(a)
        buckets.append(
            Bucket(
                start=a,
                end=b,
                change=(last.sum if last else 0.0) - (base.sum if base else 0.0),
            )
        )
    return buckets


def hours_wanted(edges_: list[float]) -> set[float]:
    """The hours whose rows answer the edges."""
    return {row_before(edge) for edge in edges_}
