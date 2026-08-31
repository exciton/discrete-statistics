"""Tests for cumulative sum construction and statistic metadata."""

from datetime import datetime, timezone

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


SECONDS_ON = "discrete_statistics:binary_sensor_grid_status_on_seconds"
COUNT_ON = "discrete_statistics:binary_sensor_grid_status_on_count"
SECONDS_OFF = "discrete_statistics:binary_sensor_grid_status_off_seconds"
SECONDS_NO_DATA = "discrete_statistics:binary_sensor_grid_status_no_data_seconds"
COUNT_NO_DATA = "discrete_statistics:binary_sensor_grid_status_no_data_count"


def test_single_hour_single_state():
    payloads = build_payloads(
        cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {}
    )
    metadata, rows, state, metric = payloads[SECONDS_ON]
    assert state == "on"
    assert metric == "seconds"
    assert metadata["source"] == "discrete_statistics"
    assert metadata["statistic_id"] == SECONDS_ON
    assert metadata["has_sum"] is True
    assert metadata["unit_of_measurement"] == "s"
    assert metadata["unit_class"] == "duration"
    assert rows == [
        {"start": datetime.fromtimestamp(T0, tz=timezone.utc), "sum": HOUR}
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
        cfg(), {("on", T0): (HOUR, 1)}, T0, T0 + HOUR, {SECONDS_ON: 500.0}
    )
    _, rows, _, _ = payloads[SECONDS_ON]
    assert rows[0]["sum"] == 500.0 + HOUR


def test_rows_are_dense_even_when_a_state_is_absent_from_an_hour():
    # "off" occurs only in the second hour, but must have a row in both.
    buckets = {
        ("on", T0): (HOUR, 0),
        ("off", T0 + HOUR): (HOUR, 1),
    }
    payloads = build_payloads(cfg(), buckets, T0, T0 + 2 * HOUR, {})
    _, off_rows, _, _ = payloads[SECONDS_OFF]
    assert len(off_rows) == 2
    assert off_rows[0]["sum"] == 0.0
    assert off_rows[1]["sum"] == HOUR


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
    metadata, _, _, _ = payloads[SECONDS_ON]
    assert "binary_sensor.grid_status" in metadata["name"]


def test_configured_name_is_used():
    payloads = build_payloads(
        cfg(name="Grid Status"), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {}
    )
    metadata, _, _, _ = payloads[SECONDS_ON]
    assert metadata["name"] == "Grid Status: on (duration)"


def test_start_times_are_utc_aware():
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    _, rows, _, _ = payloads[SECONDS_ON]
    assert rows[0]["start"].tzinfo is not None


def test_no_data_gets_a_duration_but_no_count():
    """Nothing transitions INTO no_data, so its count is structurally zero."""
    payloads = build_payloads(
        cfg(), {(NO_DATA, T0): (HOUR, 0), ("on", T0 + HOUR): (HOUR, 1)},
        T0,
        T0 + 2 * HOUR,
        {},
    )
    assert SECONDS_NO_DATA in payloads
    assert COUNT_NO_DATA not in payloads
    # Every other state keeps both metrics.
    assert COUNT_ON in payloads


def test_no_data_count_is_not_emitted_even_when_already_known():
    """A known-states set naming no_data must not resurrect its count."""
    payloads = build_payloads(
        cfg(), {}, T0, T0 + HOUR, {}, frozenset({NO_DATA, "on"})
    )
    assert SECONDS_NO_DATA in payloads
    assert COUNT_NO_DATA not in payloads
