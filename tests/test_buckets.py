"""Bucket edges and the change between them, from rows at the edges alone."""

from datetime import datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from custom_components.discrete_statistics.buckets import (
    Bucket,
    Row,
    cut,
    edges,
    hours_wanted,
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


def test_hours_wanted_are_each_edge_and_the_hour_before():
    assert hours_wanted([0.0, 7200.0]) == {-3600.0, 0.0, 3600.0, 7200.0}


def _lookups(rows: list[Row]):
    """Lookups over a sorted list of rows, counting the calls."""
    calls = {"before": 0, "after": 0}

    def newest_before(edge: float) -> Row | None:
        calls["before"] += 1
        found = [r for r in rows if r.start < edge]
        return found[-1] if found else None

    def oldest_at_or_after(edge: float) -> Row | None:
        calls["after"] += 1
        found = [r for r in rows if r.start >= edge]
        return found[0] if found else None

    return newest_before, oldest_at_or_after, calls


def _dense(hours: range, per_hour: float = 0.25) -> list[Row]:
    return [Row(h * HOUR, per_hour * (i + 1)) for i, h in enumerate(hours)]


def _at(rows: list[Row], edges_: list[float]) -> dict[float, Row]:
    wanted = hours_wanted(edges_)
    return {r.start: r for r in rows if r.start in wanted}


class TestCut:
    def test_dense_rows_need_no_lookups(self):
        rows = _dense(range(48))
        e = [0.0, 24 * HOUR, 48 * HOUR]
        before, after, calls = _lookups(rows)

        result = cut(e, _at(rows, e), before, after)

        assert result == [
            Bucket(0.0, 24 * HOUR, 6.0),
            Bucket(24 * HOUR, 48 * HOUR, 6.0),
        ]
        assert calls == {"before": 1, "after": 0}

    def test_the_first_bucket_starts_from_zero(self):
        # The one lookup above is for the sum before the first edge; here
        # the series begins later than that, so the base is zero.
        rows = _dense(range(10, 48))
        e = [0.0, 24 * HOUR, 48 * HOUR]
        before, after, _ = _lookups(rows)

        result = cut(e, _at(rows, e), before, after)

        assert result[0] == Bucket(10 * HOUR, 24 * HOUR, 14 * 0.25)
        assert result[1] == Bucket(24 * HOUR, 48 * HOUR, 24 * 0.25)

    def test_a_base_before_the_first_edge_is_looked_up(self):
        rows = [
            Row(-5 * HOUR, 100.0),
            *[Row(h * HOUR, 100.0 + h + 1) for h in range(24)],
        ]
        e = [0.0, 24 * HOUR]
        before, after, _ = _lookups(rows)

        result = cut(e, _at(rows, e), before, after)

        assert result == [Bucket(0.0, 24 * HOUR, 24.0)]

    def test_a_hole_straddling_an_edge_lands_in_neither_bucket(self):
        # Rows for hours 0-19 and 30-47: the hole 20-29 crosses the edge at
        # 24. The left bucket ends at hour 20, the right starts at 30, and
        # the change across the hole is zero because the sum carried.
        rows = [Row(h * HOUR, float(h + 1)) for h in range(20)] + [
            Row(h * HOUR, 20.0 + (h - 29)) for h in range(30, 48)
        ]
        e = [0.0, 24 * HOUR, 48 * HOUR]
        before, after, calls = _lookups(rows)

        result = cut(e, _at(rows, e), before, after)

        assert result == [
            Bucket(0.0, 20 * HOUR, 20.0),
            Bucket(30 * HOUR, 48 * HOUR, 18.0),
        ]
        assert calls == {"before": 2, "after": 1}

    def test_a_hole_inside_a_bucket_stays_in_its_span(self):
        rows = [Row(h * HOUR, float(h + 1)) for h in range(10)] + [
            Row(h * HOUR, 10.0 + (h - 13)) for h in range(14, 24)
        ]
        e = [0.0, 24 * HOUR]
        before, after, _ = _lookups(rows)

        assert cut(e, _at(rows, e), before, after) == [Bucket(0.0, 24 * HOUR, 20.0)]

    def test_an_empty_bucket_is_left_out(self):
        rows = _dense(range(24)) + [
            Row(h * HOUR, 6.0 + 0.25 * (h - 47)) for h in range(48, 72)
        ]
        e = [0.0, 24 * HOUR, 48 * HOUR, 72 * HOUR]
        before, after, _ = _lookups(rows)

        result = cut(e, _at(rows, e), before, after)

        assert [b.start for b in result] == [0.0, 48 * HOUR]
        assert result[1].change == 6.0

    def test_a_long_run_of_empty_edges_costs_one_lookup_each_way(self):
        # A statistic that begins in the last of ten buckets: every earlier
        # edge misses on both sides, and one answer covers them all.
        rows = _dense(range(9 * 24, 10 * 24))
        e = [float(d * 24 * HOUR) for d in range(11)]
        before, after, calls = _lookups(rows)

        result = cut(e, _at(rows, e), before, after)

        assert result == [Bucket(9 * 24 * HOUR, 10 * 24 * HOUR, 6.0)]
        assert calls == {"before": 1, "after": 1}

    def test_no_rows_at_all(self):
        before, after, _ = _lookups([])
        assert cut([0.0, HOUR], {}, before, after) == []

    def test_fewer_than_two_edges(self):
        before, after, _ = _lookups(_dense(range(2)))
        assert cut([0.0], {}, before, after) == []


@pytest.mark.parametrize("period", ["hour", "day", "week", "month", "year"])
def test_edges_are_increasing(period):
    result = edges(ts(2025, 1, 15), ts(2026, 9, 9), period, TZ)
    assert all(a < b for a, b in pairwise(result))
