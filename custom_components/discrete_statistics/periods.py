"""The edges of a named period - today, last month, all time.

A sensor reads the sum at the period's start against the sum at its end
(or the newest row, when the end is still ahead), so a period is two
edges. They are aligned as the card's buckets and the recorder's own
reduction are: local midnight, Monday weeks, in the instance's timezone.
"""

from __future__ import annotations

import math
from datetime import tzinfo
from typing import Literal, get_args

from . import buckets

Period = Literal[
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_year",
    "last_year",
    "all_time",
]
PERIODS: tuple[str, ...] = get_args(Period)

_UNIT: dict[str, buckets.Period] = {
    "today": "day",
    "yesterday": "day",
    "this_week": "week",
    "last_week": "week",
    "this_month": "month",
    "last_month": "month",
    "this_year": "year",
    "last_year": "year",
}
_PREVIOUS = frozenset({"yesterday", "last_week", "last_month", "last_year"})


def bounds(period: Period, now: float, tz: tzinfo) -> tuple[float | None, float]:
    """The [start, end) of a period as of now.

    All time has no start of its own - it begins where the series does,
    which only the recorder knows - and no end.
    """
    if period == "all_time":
        return None, math.inf
    unit = _UNIT[period]
    start = buckets.floor(now, unit, tz)
    end = buckets.after(start, unit, tz)
    if period in _PREVIOUS:
        # The previous period ends where this one starts. Its start is the
        # floor of the instant before, which crosses a month or year of any
        # length without arithmetic on it.
        end, start = start, buckets.floor(start - 1, unit, tz)
    return start, end
