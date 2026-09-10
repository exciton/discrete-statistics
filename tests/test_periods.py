"""A named period's edges, in the instance's timezone."""

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.discrete_statistics.periods import (
    PERIODS,
    bounds,
    custom_window,
    is_custom,
    is_rolling,
)

TZ = ZoneInfo("Australia/Sydney")


def local(*args: int, tz=TZ) -> float:
    return datetime(*args, tzinfo=tz).timestamp()


# A Wednesday afternoon.
NOW = local(2026, 9, 9, 15, 30)


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        ("today", local(2026, 9, 9), local(2026, 9, 10)),
        ("yesterday", local(2026, 9, 8), local(2026, 9, 9)),
        ("this_week", local(2026, 9, 7), local(2026, 9, 14)),
        ("last_week", local(2026, 8, 31), local(2026, 9, 7)),
        ("this_month", local(2026, 9, 1), local(2026, 10, 1)),
        ("last_month", local(2026, 8, 1), local(2026, 9, 1)),
        ("this_year", local(2026, 1, 1), local(2027, 1, 1)),
        ("last_year", local(2025, 1, 1), local(2026, 1, 1)),
        ("last_hour", NOW - 3600, NOW),
        ("last_24_hours", NOW - 24 * 3600, NOW),
        ("last_7_days", NOW - 7 * 24 * 3600, NOW),
        ("last_30_days", NOW - 30 * 24 * 3600, NOW),
        ("last_365_days", NOW - 365 * 24 * 3600, NOW),
    ],
)
def test_bounds(period, start, end):
    assert bounds(period, NOW, TZ) == (start, end)


def test_all_time_has_no_start_and_never_ends():
    assert bounds("all_time", NOW, TZ) == (None, math.inf)


def test_every_period_is_answered():
    for period in PERIODS:
        if is_custom(period):
            with pytest.raises(ValueError):
                bounds(period, NOW, TZ)
            continue
        start, end = bounds(period, NOW, TZ)
        assert start is None or start < end


@pytest.mark.parametrize(
    ("now", "hours"),
    [
        # Clocks go forward on 2026-10-04 in Sydney: a 23-hour day.
        (local(2026, 10, 4, 12), 23),
        # And back on 2026-04-05: 25 hours.
        (local(2026, 4, 5, 12), 25),
    ],
)
def test_a_day_that_changes_clocks_is_its_real_length(now, hours):
    start, end = bounds("today", now, TZ)
    assert (end - start) / 3600 == hours


def test_edges_fall_where_the_zone_puts_midnight():
    kolkata = ZoneInfo("Asia/Kolkata")
    start, end = bounds("today", local(2026, 9, 9, 12, tz=kolkata), kolkata)
    assert start % 3600 == 1800
    assert end % 3600 == 1800


def test_a_rolling_window_is_its_length_across_a_clock_change():
    # The calendar day is 23 hours here; the rolling day is not.
    start, end = bounds("last_24_hours", local(2026, 10, 4, 12), TZ)
    assert (end - start) / 3600 == 24


def test_which_periods_roll_and_which_are_custom():
    assert [p for p in PERIODS if is_rolling(p)] == [
        "last_hour",
        "last_24_hours",
        "last_7_days",
        "last_30_days",
        "last_365_days",
    ]
    assert [p for p in PERIODS if is_custom(p)] == ["custom"]


@pytest.mark.parametrize(
    ("start", "end", "duration", "window"),
    [
        (100.0, 500.0, None, (100.0, 500.0)),
        (100.0, None, 60.0, (100.0, 160.0)),
        (None, 500.0, 60.0, (440.0, 500.0)),
        (100.0, None, None, (100.0, 1000.0)),
        (None, None, None, None),
        (None, 500.0, None, None),
        (None, None, 60.0, None),
        (100.0, 500.0, 60.0, None),
    ],
)
def test_custom_window_takes_two_of_three_or_a_start_alone(
    start, end, duration, window
):
    assert custom_window(start, end, duration, now=1000.0) == window
