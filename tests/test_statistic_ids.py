"""Tests for statistic ID construction."""

import re

import pytest

from custom_components.discrete_stats.const import METRIC_COUNT, METRIC_SECONDS
from custom_components.discrete_stats.statistic_ids import (
    InvalidStatisticIdError,
    build,
)

# Copied verbatim from homeassistant.components.recorder.statistics
VALID_STATISTIC_ID = re.compile(
    r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$"
)


def test_builds_expected_id():
    assert (
        build("binary_sensor.grid_status", "off", METRIC_SECONDS)
        == "discrete_stats:binary_sensor_grid_status_off_seconds"
    )


def test_count_metric():
    assert (
        build("binary_sensor.grid_status", "on", METRIC_COUNT)
        == "discrete_stats:binary_sensor_grid_status_on_count"
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
    for metric in (METRIC_SECONDS, METRIC_COUNT):
        result = build(entity_id, state, metric)
        assert VALID_STATISTIC_ID.match(result), result


def test_no_double_underscores_from_awkward_states():
    # A state with a trailing separator would naively produce "__".
    result = build("sensor.x", "on ", METRIC_SECONDS)
    assert "__" not in result
    assert VALID_STATISTIC_ID.match(result)


def test_state_that_slugifies_to_nothing_is_rejected():
    with pytest.raises(InvalidStatisticIdError):
        build("sensor.x", "!!!", METRIC_SECONDS)
