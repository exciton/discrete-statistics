"""Tests for statistic ID construction."""

import re

import pytest

from custom_components.discrete_statistics.const import METRIC_COUNT, METRIC_DURATION
from custom_components.discrete_statistics.statistic_ids import (
    InvalidStatisticIdError,
    belongs_to,
    build,
    is_blank,
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
    # Case-folded, like the rest of the slug.
    assert (
        build("binary_sensor.grid_status", "Off", METRIC_DURATION)
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
        ("binary_sensor.grid_status", "off"),
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


def test_nesting_entities_do_not_collide():
    """The reason the state is one token, with underscores stripped from it
    and never from the entity.

    Under a multi-token state these two produce the identical ID and write
    to the same series, silently interleaving two entities' data.
    """
    a = build("climate.zone", "heat_cool", METRIC_COUNT)
    b = build("climate.zone_heat", "cool", METRIC_COUNT)
    assert a != b
    assert a == "discrete_statistics:climate_zone_heatcool_count"
    assert b == "discrete_statistics:climate_zone_heat_cool_count"


def test_parse_reads_back_from_the_right():
    assert parse("discrete_statistics:climate_zone_heatcool_count") == (
        "climate_zone",
        "heatcool",
        "count",
    )
    assert parse("discrete_statistics:climate_zone_heat_cool_count") == (
        "climate_zone_heat",
        "cool",
        "count",
    )


def test_parse_rejects_ids_that_are_not_ours():
    assert parse("sensor.living_room_temperature") is None
    assert parse("other_domain:climate_zone_heat_count") is None
    # A metric that is not one of ours: renamed by hand, or from elsewhere.
    assert parse("discrete_statistics:climate_zone_heat_seconds") is None
    assert parse("discrete_statistics:tooshort") is None


def test_a_state_named_like_a_metric_still_parses():
    statistic_id = build("sensor.x", "count", METRIC_COUNT)
    assert parse(statistic_id) == ("sensor_x", "count", "count")


def test_belongs_to_distinguishes_nesting_entities():
    a = build("climate.zone", "heat_cool", METRIC_COUNT)
    b = build("climate.zone_heat", "cool", METRIC_COUNT)
    assert belongs_to(a, "climate.zone")
    assert not belongs_to(a, "climate.zone_heat")
    assert belongs_to(b, "climate.zone_heat")
    assert not belongs_to(b, "climate.zone")


def test_states_differing_only_by_separators_share_a_token():
    """Deliberate: they merge into one statistic, like a free state map."""
    assert state_token("heat_cool") == state_token("heatcool") == "heatcool"
    assert state_token("Heat Cool") == "heatcool"


def test_is_blank_is_exactly_the_states_with_no_letters_or_digits():
    assert not is_blank("on")
    assert not is_blank("unknown")
    assert not is_blank("heat_cool")
    assert not is_blank("3")
    assert is_blank("")
    assert is_blank("   ")
    assert is_blank("!!!")
    assert is_blank("-")


def test_build_refuses_a_blank_state():
    for state in ("", "!!!", "-"):
        with pytest.raises(InvalidStatisticIdError):
            build("sensor.x", state, METRIC_DURATION)


def test_a_state_that_normalises_to_unknown_is_a_name_not_a_blank():
    """`__unknown__` spells unknown; `!!!` spells nothing.

    slugify answers "unknown" for both, which is why blankness is judged on
    the input. The first is a real name and merges with `unknown` the same
    way `heat_cool` merges with `heatcool`; only the second has no name.
    """
    assert not is_blank("__unknown__")
    assert not is_blank(" unknown ")
    assert not is_blank("Unknown!")
    assert is_blank("!!!")
    assert is_blank("🙂")

    assert build("sensor.x", "__unknown__", METRIC_DURATION) == build(
        "sensor.x", "unknown", METRIC_DURATION
    )
