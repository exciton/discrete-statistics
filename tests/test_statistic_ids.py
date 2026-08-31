"""Tests for statistic ID construction."""

import re

import pytest

from custom_components.discrete_statistics.const import METRIC_COUNT, METRIC_DURATION
from custom_components.discrete_statistics.statistic_ids import (
    InvalidStatisticIdError,
    build,
)

# Copied verbatim from homeassistant.components.recorder.statistics
VALID_STATISTIC_ID = re.compile(
    r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$"
)


def test_builds_expected_id():
    assert (
        build("binary_sensor.grid_status", "off", METRIC_DURATION)
        == "discrete_statistics:binary_sensor_grid_status_off_duration"
    )


def test_count_metric():
    assert (
        build("binary_sensor.grid_status", "on", METRIC_COUNT)
        == "discrete_statistics:binary_sensor_grid_status_on_count"
    )


@pytest.mark.parametrize(
    ("entity_id", "state"),
    [
        ("sensor.heat_pump_hvac_action", "heating"),
        ("binary_sensor.grid_status", "no_data"),
        ("sensor.a", "b"),
        ("climate.living_room", "Heat Cool"),
        ("sensor.x", "état"),
    ],
)
def test_all_ids_are_valid(entity_id, state):
    for metric in (METRIC_DURATION, METRIC_COUNT):
        result = build(entity_id, state, metric)
        assert VALID_STATISTIC_ID.match(result), result


def test_no_double_underscores_from_awkward_states():
    # A state with a trailing separator would naively produce "__".
    result = build("sensor.x", "on ", METRIC_DURATION)
    assert "__" not in result
    assert VALID_STATISTIC_ID.match(result)


def test_state_that_slugifies_to_nothing_is_rejected():
    with pytest.raises(InvalidStatisticIdError):
        build("sensor.x", "!!!", METRIC_DURATION)


def test_transliterable_non_ascii_state_is_accepted():
    result = build("sensor.x", "日本", METRIC_DURATION)
    assert VALID_STATISTIC_ID.match(result), result


def test_genuine_unknown_state_is_accepted():
    result = build("sensor.x", "unknown", METRIC_DURATION)
    assert result.endswith("_unknown_duration")
