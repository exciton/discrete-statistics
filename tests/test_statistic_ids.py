"""Tests for statistic ID construction."""

import re

import pytest

from custom_components.discrete_statistics.const import METRIC_COUNT, METRIC_DURATION
from custom_components.discrete_statistics.statistic_ids import (
    InvalidStatisticIdError,
    belongs_to,
    build,
    parse,
    state_token,
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


def test_the_state_is_always_one_token():
    """Underscores are stripped from the state, never from the entity."""
    assert (
        build("climate.kitchen", "heat_cool", METRIC_COUNT)
        == "discrete_statistics:climate_kitchen_heatcool_count"
    )
    assert (
        build("binary_sensor.grid_status", "no_data", METRIC_DURATION)
        == "discrete_statistics:binary_sensor_grid_status_nodata_duration"
    )


def test_nesting_entities_no_longer_collide():
    """The reason the state is one token.

    Under a multi-token state these two produce the identical ID and write
    to the same series, silently interleaving two entities' data.
    """
    a = build("climate.kitchen", "heat_cool", METRIC_COUNT)
    b = build("climate.kitchen_heat", "cool", METRIC_COUNT)
    assert a != b
    assert a == "discrete_statistics:climate_kitchen_heatcool_count"
    assert b == "discrete_statistics:climate_kitchen_heat_cool_count"


def test_parse_reads_back_from_the_right():
    assert parse("discrete_statistics:climate_kitchen_heatcool_count") == (
        "climate_kitchen",
        "heatcool",
        "count",
    )
    assert parse("discrete_statistics:climate_kitchen_heat_cool_count") == (
        "climate_kitchen_heat",
        "cool",
        "count",
    )


def test_parse_rejects_ids_that_are_not_ours():
    assert parse("sensor.living_room_temperature") is None
    assert parse("other_domain:climate_kitchen_heat_count") is None
    # A metric that is not one of ours: renamed by hand, or from elsewhere.
    assert parse("discrete_statistics:climate_kitchen_heat_seconds") is None
    assert parse("discrete_statistics:tooshort") is None


def test_a_state_named_like_a_metric_still_parses():
    statistic_id = build("sensor.x", "count", METRIC_COUNT)
    assert parse(statistic_id) == ("sensor_x", "count", "count")


def test_belongs_to_distinguishes_nesting_entities():
    a = build("climate.kitchen", "heat_cool", METRIC_COUNT)
    b = build("climate.kitchen_heat", "cool", METRIC_COUNT)
    assert belongs_to(a, "climate.kitchen")
    assert not belongs_to(a, "climate.kitchen_heat")
    assert belongs_to(b, "climate.kitchen_heat")
    assert not belongs_to(b, "climate.kitchen")


def test_states_differing_only_by_separators_share_a_token():
    """Deliberate: they merge into one statistic, like a free state map."""
    assert state_token("heat_cool") == state_token("heatcool") == "heatcool"
    assert state_token("Heat Cool") == "heatcool"
