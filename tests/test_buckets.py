"""Bucket edges and the change between them, from rows at the edges alone."""

from bisect import bisect_left
from datetime import datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from custom_components.discrete_statistics.buckets import (
    Bucket,
    Row,
    cut,
    edges,
    has_row,
)
from custom_components.discrete_statistics.const import HOUR

TZ = ZoneInfo("Australia/Sydney")


def ts(*args: int) -> float:
    return datetime(*args, tzinfo=TZ).timestamp()


class TestEdges:
    def test_hours_are_utc_hour_starts(self):
        assert edges(1000.0, 8000.0, "hour", TZ) == [0.0, 3600.0, 7200.0, 10800.0]

    def test_days_start_at_local_midnight_and_snap_outward(self):
        assert edges(ts(2026, 3, 2, 15), ts(2026, 3, 4, 9), "day", TZ) == [
            ts(2026, 3, 2),
            ts(2026, 3, 3),
            ts(2026, 3, 4),
            ts(2026, 3, 5),
        ]

    def test_a_day_that_changes_clocks_is_one_edge_apart(self):
        # Sydney leaves daylight saving on 2026-04-05: that day is 25 hours.
        result = edges(ts(2026, 4, 4, 12), ts(2026, 4, 6), "day", TZ)
        assert result == [ts(2026, 4, 4), ts(2026, 4, 5), ts(2026, 4, 6)]
        assert result[2] - result[1] == 25 * HOUR

    def test_weeks_start_on_monday(self):
        # 2026-09-09 is a Wednesday.
        assert edges(ts(2026, 9, 9), ts(2026, 9, 9, 1), "week", TZ) == [
            ts(2026, 9, 7),
            ts(2026, 9, 14),
        ]

    def test_a_year_of_months_is_thirteen_edges(self):
        result = edges(ts(2025, 9, 15), ts(2026, 9, 9), "month", TZ)
        assert result[0] == ts(2025, 9, 1)
        assert result[-1] == ts(2026, 10, 1)
        assert len(result) == 14
        assert ts(2026, 2, 1) in result and ts(2026, 3, 1) in result

    def test_years(self):
        assert edges(ts(2025, 6, 1), ts(2026, 1, 2), "year", TZ) == [
            ts(2025, 1, 1),
            ts(2026, 1, 1),
            ts(2027, 1, 1),
        ]

    def test_an_end_on_an_edge_closes_there(self):
        assert edges(ts(2026, 3, 2), ts(2026, 3, 3), "day", TZ) == [
            ts(2026, 3, 2),
            ts(2026, 3, 3),
        ]


def _dense(hours: range, per_hour: float = 0.25) -> list[Row]:
    return [Row(h * HOUR, per_hour * (i + 1)) for i, h in enumerate(hours)]


def _running(rows: list[Row]) -> list[Row]:
    """The rows of a series already running when the range opens."""
    return [Row(rows[0].start - HOUR, 0.0), *rows]


H = HOUR


def _before(rows: list[Row], edges_: list[float]) -> dict[float, Row | None]:
    """What one seek statement answers: the newest row before each edge."""
    starts = [row.start for row in rows]
    return {
        edge: (rows[found - 1] if (found := bisect_left(starts, edge)) else None)
        for edge in edges_
    }


def _compiled(before: dict[float, Row | None]):
    return lambda a, b: has_row(before, a, b)


class TestCut:
    def test_change_is_between_the_sums_at_the_edges(self):
        rows = _running(_dense(range(48)))
        e = [0.0, 24 * H, 48 * H]
        assert cut(e, _before(rows, e), lambda a, b: True) == [
            Bucket(0.0, 24 * H, 6.0),
            Bucket(24 * H, 48 * H, 6.0),
        ]

    def test_a_compiled_bucket_with_no_row_is_zero_and_an_uncompiled_one_is_left_out(
        self,
    ):
        rows = [Row(23 * H, 2.0), Row(72 * H, 5.0)]
        e = [0.0, 24 * H, 48 * H, 72 * H, 96 * H]
        result = cut(e, _before(rows, e), lambda a, b: a != 24 * H)
        assert result == [
            Bucket(0.0, 24 * H, 2.0),
            Bucket(48 * H, 72 * H, 0.0),
            Bucket(72 * H, 96 * H, 3.0),
        ]

    def test_before_the_series_the_sum_is_zero(self):
        rows = [Row(30 * H, 1.5)]
        e = [0.0, 24 * H, 48 * H]
        assert cut(e, _before(rows, e), lambda a, b: True) == [
            Bucket(0.0, 24 * H, 0.0),
            Bucket(24 * H, 48 * H, 1.5),
        ]


class TestHoles:
    def test_a_hole_straddling_an_edge_is_in_neither_bucket(self):
        # Rows every hour for hours 0-19 and 30-47: the hole spans the day
        # edge at 24H. Each bucket's change covers only its own rows.
        near = [Row(h * H, 0.25 * (h + 1)) for h in range(20)]
        far = [Row(h * H, 5.0 + 0.25 * (h - 29)) for h in range(30, 48)]
        e = [0.0, 24 * H, 48 * H]
        before = _before([Row(-H, 0.0), *near, *far], e)

        assert cut(e, before, _compiled(before)) == [
            Bucket(0.0, 24 * H, pytest.approx(20 * 0.25)),
            Bucket(24 * H, 48 * H, pytest.approx(18 * 0.25)),
        ]

    def test_a_hole_inside_a_bucket_stays_inside_it(self):
        # Hours 10-13 are missing, well within the day: one bucket, its
        # change spanning the hole rather than the bucket being split.
        rows = [
            Row(h * H, 0.25 * (h + 1 if h < 10 else h - 3))
            for h in list(range(10)) + list(range(14, 24))
        ]
        e = [0.0, 24 * H]
        before = _before([Row(-H, 0.0), *rows], e)

        assert cut(e, before, _compiled(before)) == [
            Bucket(0.0, 24 * H, pytest.approx(20 * 0.25))
        ]


def test_half_past_edges_cut_the_hours_between_them():
    # Kolkata days start at 18:30 UTC, so no row starts the hour before an
    # edge: each edge resolves to the row running through it, and a day is
    # still the twenty-four rows between two such edges - the same tally an
    # hour-aligned zone gets.
    kolkata = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 6, 1, tzinfo=kolkata)
    end = datetime(2026, 6, 4, tzinfo=kolkata)
    half_past = edges(start.timestamp(), end.timestamp(), "day", kolkata)
    assert all(edge % H == 1800 for edge in half_past)

    # A different value every hour, so a bucket reading an hour early or
    # late reads a different number.
    first = (half_past[0] // H - 24) * H
    values = [0.01 * (i + 1) for i in range(24 * (len(half_past) + 1))]
    total = 0.0
    rows = []
    for i, value in enumerate(values):
        total += value
        rows.append(Row(first + i * H, total))
    before = _before(rows, half_past)

    # The row running through edge k is the (24 + 24k)th; the day after it
    # is the twenty-four values that follow.
    expected = [
        pytest.approx(sum(values[24 + 24 * k + 1 : 24 + 24 * k + 25]))
        for k in range(len(half_past) - 1)
    ]
    assert [b.change for b in cut(half_past, before, _compiled(before))] == expected


class TestHasRow:
    def test_a_row_inside_the_bucket(self):
        e = [0.0, 24 * H, 48 * H, 72 * H]
        before = _before([Row(30 * H, 1.0)], e)
        assert has_row(before, 24 * H, 48 * H)
        assert not has_row(before, 48 * H, 72 * H)
        assert not has_row(before, 0.0, 24 * H)


@pytest.mark.parametrize("period", ["hour", "day", "week", "month", "year"])
def test_edges_are_increasing(period):
    result = edges(ts(2025, 1, 15), ts(2026, 9, 9), period, TZ)
    assert all(a < b for a, b in pairwise(result))
