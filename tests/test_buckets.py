"""Bucket edges and the change between them, from rows at the edges alone."""

from datetime import datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from custom_components.discrete_statistics.buckets import (
    LOOKUP_ROWS,
    SEEK_WORTH,
    Bucket,
    Known,
    Row,
    blanks,
    cut,
    edges,
    has_row,
    hours_wanted,
    row_before,
    wants_range,
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


def test_hours_wanted_are_the_hour_before_each_edge():
    assert hours_wanted([0.0, 7200.0]) == {-3600.0, 3600.0}


def test_an_edge_at_half_past_wants_the_hour_running_through_it():
    # Kolkata is five and a half hours off UTC, so its midnight is half
    # past a UTC hour and the row holding the sum at the edge starts
    # thirty minutes before it.
    edge = datetime(2026, 6, 1, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
    assert edge % HOUR == 1800
    assert row_before(edge) == edge - 1800
    assert row_before(0.0) == -HOUR


def _dense(hours: range, per_hour: float = 0.25) -> list[Row]:
    return [Row(h * HOUR, per_hour * (i + 1)) for i, h in enumerate(hours)]


def _running(rows: list[Row]) -> list[Row]:
    """The rows of a series already running when the range opens."""
    return [Row(rows[0].start - HOUR, 0.0), *rows]


H = HOUR


def _run(newest_hour: int, count: int, per_row: float = 1.0) -> list[Row]:
    """A newest-first run of `count` rows ending at `newest_hour`."""
    return [
        Row((newest_hour - i) * H, per_row * (newest_hour - i)) for i in range(count)
    ]


class TestKnown:
    def test_a_row_at_the_hour_before_an_edge_settles_that_edge_alone(self):
        known = Known([Row(23 * H, 1.0)])
        assert known.settled(24 * H)
        assert not known.settled(48 * H)
        assert not known.settled(0.0)

    def test_a_full_run_settles_every_edge_down_to_its_oldest_row(self):
        known = Known()
        known.seek(_run(47, LOOKUP_ROWS))
        assert known.floor == (47 - LOOKUP_ROWS + 1) * H
        assert known.settled(48 * H)
        assert known.settled(40 * H)
        assert not known.settled(24 * H)

    def test_a_short_run_settles_everything(self):
        known = Known()
        known.seek(_run(47, 3))
        assert known.settled(0.0)
        known = Known()
        known.seek([])
        assert known.settled(0.0)

    def test_a_range_settles_every_edge_down_to_its_oldest_row(self):
        known = Known()
        known.ranged([Row(30 * H, 1.0), Row(35 * H, 2.0)])
        assert known.floor == 30 * H
        assert known.settled(36 * H)
        assert not known.settled(24 * H)
        known.ranged([])
        assert known.floor == 30 * H

    def test_before_is_the_newest_row_strictly_before_the_edge(self):
        known = Known([Row(10 * H, 1.0), Row(20 * H, 2.0)])
        assert known.before(20 * H) == Row(10 * H, 1.0)
        assert known.before(21 * H) == Row(20 * H, 2.0)
        assert known.before(10 * H) is None

    def test_rows_are_deduplicated_by_start(self):
        known = Known([Row(10 * H, 1.0)])
        known.add([Row(10 * H, 9.0), Row(5 * H, 0.5)])
        assert known.before(11 * H).sum == 9.0  # last write wins
        assert known.before(10 * H) == Row(5 * H, 0.5)
        assert known._starts == [5 * H, 10 * H]  # not duplicated


DAYS_60 = [d * 24 * H for d in range(61)]
MONTHS_3 = [0.0, 30 * 24 * H, 60 * 24 * H, 90 * 24 * H]


class TestBlanks:
    def test_blanks_are_the_unsettled_edges_newest_first(self):
        known = Known([Row(row_before(MONTHS_3[1]), 1.0)])
        assert blanks(known, MONTHS_3) == [MONTHS_3[3], MONTHS_3[2], MONTHS_3[0]]

    def test_nothing_is_blank_below_a_short_run(self):
        known = Known()
        known.seek(_run(5, 2))
        assert blanks(known, MONTHS_3) == []


class TestWantsRange:
    def test_a_short_run_never_wants_a_range(self):
        # Known here was not seeded by run, so a blank edge remains close
        # enough that the density math alone would want a range - the
        # length guard is what a caller passing a stale run relies on.
        known = Known()
        run = _run(0, LOOKUP_ROWS - 1)
        assert wants_range(known, [MONTHS_3[3]], run) is None

    def test_a_busy_statistic_under_monthly_edges_keeps_seeking(self):
        # Rows every hour: the three months left hold thousands of rows.
        known = Known()
        run = _run(24 * 90 - 1, LOOKUP_ROWS)
        known.seek(run)
        assert blanks(known, MONTHS_3) == [MONTHS_3[2], MONTHS_3[1], MONTHS_3[0]]
        assert wants_range(known, MONTHS_3, run) is None

    def test_a_daily_statistic_under_daily_edges_wants_the_span(self):
        # Four rows a day, 18:00 to 21:00, over sixty days.
        rows = [
            Row((d * 24 + h) * H, float(d * 4 + h - 17))
            for d in range(60)
            for h in (18, 19, 20, 21)
        ]
        known = Known()
        run = list(reversed(rows))[:LOOKUP_ROWS]
        known.seek(run)
        blank = blanks(known, DAYS_60)
        assert len(blank) == 57
        span = wants_range(known, DAYS_60, run)
        assert span == (row_before(blank[-1]), row_before(blank[0]) + H)
        # Its estimate is well under SEEK_WORTH rows per blank edge.
        density = LOOKUP_ROWS / (run[0].start - run[-1].start + H)
        assert density * (span[1] - span[0]) < SEEK_WORTH * len(blank)

    def test_nothing_blank_wants_nothing(self):
        known = Known()
        run = _run(24 * 90 - 1, LOOKUP_ROWS)
        known.seek(run)
        assert wants_range(known, [MONTHS_3[3]], run) is None


class TestCut:
    def test_change_is_between_the_sums_at_the_edges(self):
        rows = _running(_dense(range(48)))
        known = Known(rows)
        e = [0.0, 24 * H, 48 * H]
        assert cut(e, known, lambda a, b: True) == [
            Bucket(0.0, 24 * H, 6.0),
            Bucket(24 * H, 48 * H, 6.0),
        ]

    def test_a_compiled_bucket_with_no_row_is_zero_and_an_uncompiled_one_is_left_out(
        self,
    ):
        known = Known([Row(23 * H, 2.0), Row(72 * H, 5.0)])
        known.floor = -float("inf")  # every edge settled
        e = [0.0, 24 * H, 48 * H, 72 * H, 96 * H]
        result = cut(e, known, lambda a, b: a != 24 * H)
        assert result == [
            Bucket(0.0, 24 * H, 2.0),
            Bucket(48 * H, 72 * H, 0.0),
            Bucket(72 * H, 96 * H, 3.0),
        ]

    def test_before_the_series_the_sum_is_zero(self):
        known = Known([Row(30 * H, 1.5)])
        known.floor = -float("inf")
        assert cut([0.0, 24 * H, 48 * H], known, lambda a, b: True) == [
            Bucket(0.0, 24 * H, 0.0),
            Bucket(24 * H, 48 * H, 1.5),
        ]


class TestHasRow:
    def test_a_row_inside_the_bucket(self):
        known = Known([Row(30 * H, 1.0)])
        assert has_row(known, 24 * H, 48 * H)
        assert not has_row(known, 48 * H, 72 * H)
        assert not has_row(known, 0.0, 24 * H)


@pytest.mark.parametrize("period", ["hour", "day", "week", "month", "year"])
def test_edges_are_increasing(period):
    result = edges(ts(2025, 1, 15), ts(2026, 9, 9), period, TZ)
    assert all(a < b for a, b in pairwise(result))
