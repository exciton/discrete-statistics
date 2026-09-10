"""A named period's edges, in the instance's timezone."""

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.discrete_statistics.periods import PERIODS, bounds

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
    ],
)
def test_bounds(period, start, end):
    assert bounds(period, NOW, TZ) == (start, end)


def test_all_time_has_no_start_and_never_ends():
    assert bounds("all_time", NOW, TZ) == (None, math.inf)


def test_every_period_is_answered():
    for period in PERIODS:
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
