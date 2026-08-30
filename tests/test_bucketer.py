"""Tests for the pure bucketing function."""

import pytest

from custom_components.discrete_stats.bucketer import bucket, hour_start
from custom_components.discrete_stats.const import HOUR, NO_DATA

# 2026-01-01T00:00:00Z
T0 = 1767225600.0


def total_seconds(result):
    return sum(seconds for seconds, _ in result.values())


def test_hour_start_floors_to_the_hour():
    assert hour_start(T0) == T0
    assert hour_start(T0 + 59.0) == T0
    assert hour_start(T0 + HOUR) == T0 + HOUR


def test_single_state_for_whole_window():
    result = bucket("on", [], T0, T0 + HOUR)
    assert result == {("on", T0): (HOUR, 0)}


def test_transition_exactly_at_window_start_is_ignored():
    # The canonicaliser routes ts <= window_start into carried_state, so a
    # transition here is the same event that produced it — counting it would
    # double-count.
    result = bucket("on", [(T0, "off")], T0, T0 + HOUR)
    assert result == {("on", T0): (HOUR, 0)}


def test_transition_splits_the_hour():
    result = bucket("on", [(T0 + 900.0, "off")], T0, T0 + HOUR)
    assert result == {
        ("on", T0): (900.0, 0),
        ("off", T0): (2700.0, 1),
    }


def test_outage_straddling_an_hour_boundary():
    # off from :45 in hour 0 until :15 in hour 1
    result = bucket(
        "on",
        [(T0 + 2700.0, "off"), (T0 + HOUR + 900.0, "on")],
        T0,
        T0 + 2 * HOUR,
    )
    assert result[("on", T0)] == (2700.0, 0)
    assert result[("off", T0)] == (900.0, 1)
    assert result[("off", T0 + HOUR)] == (900.0, 0)
    assert result[("on", T0 + HOUR)] == (2700.0, 1)


def test_count_lands_in_the_hour_of_the_transition():
    result = bucket("on", [(T0 + HOUR + 10.0, "off")], T0, T0 + 2 * HOUR)
    assert result[("off", T0 + HOUR)][1] == 1
    assert ("off", T0) not in result


def test_multiple_transitions_within_one_hour():
    result = bucket(
        "on",
        [(T0 + 600.0, "off"), (T0 + 1200.0, "on"), (T0 + 1800.0, "off")],
        T0,
        T0 + HOUR,
    )
    assert result[("off", T0)] == (600.0 + 1800.0, 2)
    assert result[("on", T0)] == (600.0 + 600.0, 1)


def test_no_carried_state_yields_no_data():
    result = bucket(None, [(T0 + 1800.0, "on")], T0, T0 + HOUR)
    assert result[(NO_DATA, T0)] == (1800.0, 0)
    assert result[("on", T0)] == (1800.0, 1)


def test_no_carried_state_and_no_transitions_is_all_no_data():
    result = bucket(None, [], T0, T0 + 3 * HOUR)
    assert result == {
        (NO_DATA, T0): (HOUR, 0),
        (NO_DATA, T0 + HOUR): (HOUR, 0),
        (NO_DATA, T0 + 2 * HOUR): (HOUR, 0),
    }


def test_span_of_many_days_conserves_time():
    window = 72 * HOUR
    result = bucket(
        "on",
        [(T0 + 5.5 * HOUR, "off"), (T0 + 40.25 * HOUR, "on")],
        T0,
        T0 + window,
    )
    assert total_seconds(result) == pytest.approx(window)


@pytest.mark.parametrize("transition_offset", [0.5, 1.5, 23.5, 47.9])
def test_every_window_conserves_time(transition_offset):
    window = 48 * HOUR
    result = bucket(
        "on", [(T0 + transition_offset * HOUR, "off")], T0, T0 + window
    )
    assert total_seconds(result) == pytest.approx(window)


def test_transitions_outside_the_window_are_ignored():
    result = bucket(
        "on",
        [(T0 - HOUR, "off"), (T0 + 5 * HOUR, "off")],
        T0,
        T0 + HOUR,
    )
    assert result == {("on", T0): (HOUR, 0)}
