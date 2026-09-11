"""The edges of a named period - today, last month, the last 24 hours.

A sensor reads the sum at the period's start against the sum at its end
(or the newest row, when the end is still ahead), so a period is two
edges. Calendar periods are aligned as the card's buckets and the
recorder's own reduction are: local midnight, Monday weeks, in the
instance's timezone. A rolling period is a length back from now in plain
seconds, so "the last 24 hours" is 24 hours across a clock change too.
"""

from __future__ import annotations

import math
from datetime import tzinfo
from typing import Literal, get_args

from . import buckets
from .const import HOUR, PERIOD_CUSTOM

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
    "last_hour",
    "last_24_hours",
    "last_7_days",
    "last_30_days",
    "last_365_days",
    "custom",
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
_LENGTH: dict[str, float] = {
    "last_hour": HOUR,
    "last_24_hours": 24 * HOUR,
    "last_7_days": 7 * 24 * HOUR,
    "last_30_days": 30 * 24 * HOUR,
    "last_365_days": 365 * 24 * HOUR,
}


def is_rolling(period: str) -> bool:
    return period in _LENGTH


def is_custom(period: str) -> bool:
    return period == PERIOD_CUSTOM


def bounds(period: Period, now: float, tz: tzinfo) -> tuple[float | None, float]:
    """The [start, end) of a period as of now.

    All time has no start of its own - it begins where the series does,
    which only the recorder knows - and no end. A custom period has no
    bounds of its own either: its templates say, and the coordinator
    renders them.
    """
    if period == "all_time":
        return None, math.inf
    if is_custom(period):
        raise ValueError("a custom period's window comes from its templates")
    if (length := _LENGTH.get(period)) is not None:
        return now - length, now
    unit = _UNIT[period]
    start = buckets.floor(now, unit, tz)
    end = buckets.after(start, unit, tz)
    if period in _PREVIOUS:
        # The previous period ends where this one starts. Its start is the
        # floor of the instant before, which crosses a month or year of any
        # length without arithmetic on it.
        end, start = start, buckets.floor(start - 1, unit, tz)
    return start, end


def custom_window(
    start: float | None, end: float | None, duration: float | None, now: float
) -> tuple[float, float] | None:
    """The window two of start, end and duration describe; a start alone runs to now.

    None for any other combination: nothing, one of end or duration alone,
    or all three, which could disagree.
    """
    if start is not None and end is not None and duration is None:
        return start, end
    if start is not None and end is None:
        return start, now if duration is None else start + duration
    if start is None and end is not None and duration is not None:
        return end - duration, end
    return None
