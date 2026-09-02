"""Tests for cumulative sum construction and statistic metadata."""

from datetime import datetime, timezone

import pytest

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
DURATION_NO_DATA = "discrete_statistics:binary_sensor_grid_status_nodata_duration"
COUNT_NO_DATA = "discrete_statistics:binary_sensor_grid_status_nodata_count"


def test_single_hour_single_state():
    payloads = build_payloads(
        cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {}
    )
    metadata, rows = payloads[DURATION_ON]
    assert metadata["name"] == "binary_sensor.grid_status: on (duration)"
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
    metadata, rows = payloads[COUNT_ON]
    assert metadata["unit_of_measurement"] is None
    assert metadata["unit_class"] is None
    assert rows[0]["sum"] == 2


def test_sums_are_cumulative_across_hours():
    buckets = {
        ("on", T0): (HOUR, 1),
        ("on", T0 + HOUR): (HOUR, 1),
        ("on", T0 + 2 * HOUR): (HOUR, 1),
    }
    _, rows = build_payloads(cfg(), buckets, T0, T0 + 3 * HOUR, {})[COUNT_ON]
    assert [row["sum"] for row in rows] == [1, 2, 3]


def test_base_sums_continue_the_running_total():
    payloads = build_payloads(
        cfg(), {("on", T0): (HOUR, 1)}, T0, T0 + HOUR, {DURATION_ON: 500.0}
    )
    _, rows = payloads[DURATION_ON]
    assert rows[0]["sum"] == 500.0 + 1.0


def test_rows_are_dense_even_when_a_state_is_absent_from_an_hour():
    # "off" occurs only in the second hour, but must have a row in both.
    buckets = {
        ("on", T0): (HOUR, 0),
        ("off", T0 + HOUR): (HOUR, 1),
    }
    payloads = build_payloads(cfg(), buckets, T0, T0 + 2 * HOUR, {})
    _, off_rows = payloads[DURATION_OFF]
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
    for _, rows in payloads.values():
        sums = [row["sum"] for row in rows]
        assert sums == sorted(sums)


def test_name_defaults_to_entity_id_when_not_configured():
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    metadata, _ = payloads[DURATION_ON]
    assert "binary_sensor.grid_status" in metadata["name"]


def test_configured_name_is_used():
    payloads = build_payloads(
        cfg(name="Grid Status"), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {}
    )
    metadata, _ = payloads[DURATION_ON]
    assert metadata["name"] == "Grid Status: on (duration)"


def test_start_times_are_utc_aware():
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    _, rows = payloads[DURATION_ON]
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
    """An existing no_data count must not be kept alive by density."""
    payloads = build_payloads(
        cfg(),
        {},
        T0,
        T0 + HOUR,
        {},
        {
            DURATION_NO_DATA: "x: no_data (duration)",
            COUNT_NO_DATA: "x: no_data (count)",
            DURATION_ON: "x: on (duration)",
        },
    )
    assert DURATION_NO_DATA in payloads
    assert COUNT_NO_DATA not in payloads


def test_metadata_declares_an_arithmetic_mean_alongside_the_sum():
    """A statistic carries both, and the two answer different questions."""
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    metadata, _ = payloads[DURATION_ON]
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

    _, duration_rows = payloads[DURATION_ON]
    assert [row["sum"] for row in duration_rows] == [1.0, 1.5, 2.5]
    assert [row["mean"] for row in duration_rows] == [1.0, 0.5, 1.0]
    assert [row["min"] for row in duration_rows] == [1.0, 0.5, 1.0]
    assert [row["max"] for row in duration_rows] == [1.0, 0.5, 1.0]

    _, count_rows = payloads[COUNT_ON]
    assert [row["sum"] for row in count_rows] == [1, 4, 5]
    assert [row["mean"] for row in count_rows] == [1, 3, 1]


def test_a_quiet_hour_gets_a_zero_mean_not_a_missing_one():
    """A state seen elsewhere in the window still gets a zero-valued hour.

    `_reduce_statistics` skips rows whose mean is None, so an omitted hour
    would silently raise a day's average by leaving out the quiet hours.
    Both states are in the buckets here - see the test below for the harder
    case, where the statistic is known only from `existing`.
    """
    buckets = {("on", T0): (HOUR, 1), ("off", T0 + HOUR): (HOUR, 1)}
    payloads = build_payloads(
        cfg(),
        buckets,
        T0,
        T0 + 2 * HOUR,
        {},
        {DURATION_ON: "x: on (duration)", DURATION_OFF: "x: off (duration)"},
    )
    _, on_rows = payloads[DURATION_ON]
    assert [row["mean"] for row in on_rows] == [1.0, 0.0]


def test_base_sums_do_not_leak_into_the_mean():
    """A carried-over base belongs to the sum alone."""
    payloads = build_payloads(
        cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {DURATION_ON: 500.0}
    )
    _, rows = payloads[DURATION_ON]
    assert rows[0]["sum"] == 501.0
    assert rows[0]["mean"] == 1.0


DURATION_HEATCOOL = "discrete_statistics:binary_sensor_grid_status_heatcool_duration"
COUNT_HEATCOOL = "discrete_statistics:binary_sensor_grid_status_heatcool_count"


def test_states_sharing_a_token_merge_rather_than_overwrite():
    """`heat_cool` and `heatcool` are one statistic, and must add.

    Keying payloads by raw state would let the second state replace the
    first outright - same base sum, its own values only - and the hour
    would stop totalling wall-clock time. Merging is the deliberate
    behaviour; losing half the hour is not.
    """
    buckets = {
        ("heat_cool", T0): (HOUR * 0.4, 1),
        ("heatcool", T0): (HOUR * 0.6, 2),
    }
    payloads = build_payloads(cfg(), buckets, T0, T0 + HOUR, {})

    assert [k for k in payloads if k.endswith("_duration")] == [DURATION_HEATCOOL]
    _, duration_rows = payloads[DURATION_HEATCOOL]
    _, count_rows = payloads[COUNT_HEATCOOL]
    assert duration_rows[0]["sum"] == pytest.approx(1.0)
    assert count_rows[0]["sum"] == 3


def test_a_rename_reaches_a_state_absent_from_the_window():
    """The display half is swapped; the readable state survives.

    A state the entity has not been in for months would otherwise keep the
    old name forever, because its statistic is only carried, never rebuilt
    from a bucket.
    """
    payloads = build_payloads(
        cfg("Grid Status"),
        {},
        T0,
        T0 + HOUR,
        {},
        {DURATION_ON: "Old Name: on (duration)"},
    )
    metadata, _ = payloads[DURATION_ON]
    assert metadata["name"] == "Grid Status: on (duration)"


def test_a_rename_survives_a_colon_in_the_old_display_name():
    """Split on the last separator, not the first."""
    payloads = build_payloads(
        cfg("Grid"),
        {},
        T0,
        T0 + HOUR,
        {},
        {DURATION_ON: "Shed: Grid Status: on (duration)"},
    )
    metadata, _ = payloads[DURATION_ON]
    assert metadata["name"] == "Grid: on (duration)"


def test_an_unrecognisable_name_is_left_alone_rather_than_mangled():
    payloads = build_payloads(
        cfg("Grid"), {}, T0, T0 + HOUR, {}, {DURATION_ON: "renamed by hand"}
    )
    metadata, _ = payloads[DURATION_ON]
    assert metadata["name"] == "renamed by hand"


def test_a_statistic_known_only_from_existing_is_carried_at_its_base():
    """The density case that has no bucket at all.

    Nothing of this statistic's state occurs in the window, so its rows can
    only come from `existing`. It must still get one per hour, holding its
    cumulative sum flat and reporting a zero hourly value - otherwise the
    next window finds no row in the hour before it, restarts the sum at zero
    and the series goes backwards for good.
    """
    payloads = build_payloads(
        cfg(),
        {("on", T0): (HOUR, 1)},
        T0,
        T0 + 2 * HOUR,
        {DURATION_OFF: 7.5},
        {DURATION_OFF: "x: off (duration)"},
    )
    _, off_rows = payloads[DURATION_OFF]
    assert [row["sum"] for row in off_rows] == [7.5, 7.5]
    assert [row["mean"] for row in off_rows] == [0.0, 0.0]
    assert [row["max"] for row in off_rows] == [0.0, 0.0]
