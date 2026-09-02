"""Tests for cumulative sum construction and statistic metadata."""

from datetime import datetime, timezone

from homeassistant.components.recorder.models import StatisticMeanType

from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import HOUR, NO_DATA
from custom_components.discrete_statistics.payload import build_payloads

T0 = 1767225600.0


def cfg(name=None):
    return EntityConfig(
        entity_id="binary_sensor.grid_status",
        name=name,
        default="record_known",
        states={},
    )


DURATION_ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
COUNT_ON = "discrete_statistics:binary_sensor_grid_status_on_count"
DURATION_OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"
DURATION_NO_DATA = "discrete_statistics:binary_sensor_grid_status_no_data_duration"
COUNT_NO_DATA = "discrete_statistics:binary_sensor_grid_status_no_data_count"


def test_single_hour_single_state():
    payloads = build_payloads(
        cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {}
    )
    metadata, rows, state, metric = payloads[DURATION_ON]
    assert state == "on"
    assert metric == "duration"
    assert metadata["source"] == "discrete_statistics"
    assert metadata["statistic_id"] == DURATION_ON
    assert metadata["has_sum"] is True
    assert metadata["unit_of_measurement"] == "h"
    assert metadata["unit_class"] == "duration"
    assert rows == [
        {
            "start": datetime.fromtimestamp(T0, tz=timezone.utc),
            "sum": 1.0,
            "mean": 1.0,
            "min": 1.0,
            "max": 1.0,
        }
    ]


def test_count_metadata_has_no_unit():
    payloads = build_payloads(
        cfg(), {("on", T0): (HOUR, 2)}, T0, T0 + HOUR, {}
    )
    metadata, rows, _, _ = payloads[COUNT_ON]
    assert metadata["unit_of_measurement"] is None
    assert metadata["unit_class"] is None
    assert rows[0]["sum"] == 2


def test_sums_are_cumulative_across_hours():
    buckets = {
        ("on", T0): (HOUR, 1),
        ("on", T0 + HOUR): (HOUR, 1),
        ("on", T0 + 2 * HOUR): (HOUR, 1),
    }
    _, rows, _, _ = build_payloads(cfg(), buckets, T0, T0 + 3 * HOUR, {})[COUNT_ON]
    assert [row["sum"] for row in rows] == [1, 2, 3]


def test_base_sums_continue_the_running_total():
    payloads = build_payloads(
        cfg(), {("on", T0): (HOUR, 1)}, T0, T0 + HOUR, {DURATION_ON: 500.0}
    )
    _, rows, _, _ = payloads[DURATION_ON]
    assert rows[0]["sum"] == 500.0 + 1.0


def test_rows_are_dense_even_when_a_state_is_absent_from_an_hour():
    # "off" occurs only in the second hour, but must have a row in both.
    buckets = {
        ("on", T0): (HOUR, 0),
        ("off", T0 + HOUR): (HOUR, 1),
    }
    payloads = build_payloads(cfg(), buckets, T0, T0 + 2 * HOUR, {})
    _, off_rows, _, _ = payloads[DURATION_OFF]
    assert len(off_rows) == 2
    assert off_rows[0]["sum"] == 0.0
    assert off_rows[1]["sum"] == 1.0


def test_sums_never_decrease():
    buckets = {
        ("on", T0): (HOUR, 1),
        ("off", T0 + HOUR): (HOUR, 1),
        ("on", T0 + 2 * HOUR): (HOUR, 1),
    }
    payloads = build_payloads(cfg(), buckets, T0, T0 + 3 * HOUR, {})
    for _, rows, _, _ in payloads.values():
        sums = [row["sum"] for row in rows]
        assert sums == sorted(sums)


def test_name_defaults_to_entity_id_when_not_configured():
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    metadata, _, _, _ = payloads[DURATION_ON]
    assert "binary_sensor.grid_status" in metadata["name"]


def test_configured_name_is_used():
    payloads = build_payloads(
        cfg(name="Grid Status"), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {}
    )
    metadata, _, _, _ = payloads[DURATION_ON]
    assert metadata["name"] == "Grid Status: on (duration)"


def test_start_times_are_utc_aware():
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    _, rows, _, _ = payloads[DURATION_ON]
    assert rows[0]["start"].tzinfo is not None


def test_no_data_gets_a_duration_but_no_count():
    """Nothing transitions INTO no_data, so its count is structurally zero."""
    payloads = build_payloads(
        cfg(), {(NO_DATA, T0): (HOUR, 0), ("on", T0 + HOUR): (HOUR, 1)},
        T0,
        T0 + 2 * HOUR,
        {},
    )
    assert DURATION_NO_DATA in payloads
    assert COUNT_NO_DATA not in payloads
    # Every other state keeps both metrics.
    assert COUNT_ON in payloads


def test_no_data_count_is_not_emitted_even_when_already_known():
    """A known-states set naming no_data must not resurrect its count."""
    payloads = build_payloads(
        cfg(), {}, T0, T0 + HOUR, {}, frozenset({NO_DATA, "on"})
    )
    assert DURATION_NO_DATA in payloads
    assert COUNT_NO_DATA not in payloads


def test_metadata_declares_an_arithmetic_mean_alongside_the_sum():
    """A statistic carries both, and the two answer different questions."""
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    metadata, _, _, _ = payloads[DURATION_ON]
    assert metadata["has_sum"] is True
    assert metadata["has_mean"] is True
    assert metadata["mean_type"] == StatisticMeanType.ARITHMETIC


def test_mean_min_and_max_are_the_hourly_value_not_the_running_sum():
    """The sum accumulates; mean/min/max describe the hour on its own.

    Writing the running sum into `mean` would make a day's average climb
    forever instead of reporting the average hour.
    """
    buckets = {
        ("on", T0): (HOUR, 1),
        ("on", T0 + HOUR): (HOUR / 2, 3),
        ("on", T0 + 2 * HOUR): (HOUR, 1),
    }
    payloads = build_payloads(cfg(), buckets, T0, T0 + 3 * HOUR, {})

    _, duration_rows, _, _ = payloads[DURATION_ON]
    assert [row["sum"] for row in duration_rows] == [1.0, 1.5, 2.5]
    assert [row["mean"] for row in duration_rows] == [1.0, 0.5, 1.0]
    assert [row["min"] for row in duration_rows] == [1.0, 0.5, 1.0]
    assert [row["max"] for row in duration_rows] == [1.0, 0.5, 1.0]

    _, count_rows, _, _ = payloads[COUNT_ON]
    assert [row["sum"] for row in count_rows] == [1, 4, 5]
    assert [row["mean"] for row in count_rows] == [1, 3, 1]


def test_a_quiet_hour_gets_a_zero_mean_not_a_missing_one():
    """Density is what makes the average an average over every hour.

    `_reduce_statistics` skips rows whose mean is None, so an omitted hour
    would silently raise a day's average by leaving out the quiet hours.
    """
    buckets = {("on", T0): (HOUR, 1), ("off", T0 + HOUR): (HOUR, 1)}
    payloads = build_payloads(
        cfg(), buckets, T0, T0 + 2 * HOUR, {}, frozenset({"on", "off"})
    )
    _, on_rows, _, _ = payloads[DURATION_ON]
    assert [row["mean"] for row in on_rows] == [1.0, 0.0]


def test_base_sums_do_not_leak_into_the_mean():
    """A carried-over base belongs to the sum alone."""
    payloads = build_payloads(
        cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {DURATION_ON: 500.0}
    )
    _, rows, _, _ = payloads[DURATION_ON]
    assert rows[0]["sum"] == 501.0
    assert rows[0]["mean"] == 1.0
