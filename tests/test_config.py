"""Tests for configuration parsing and state dispositions."""

import pytest
import voluptuous as vol

from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from custom_components.discrete_statistics.config import (
    CONF_DEFAULT,
    CONF_MIN_DURATION,
    CONFIG_SCHEMA,
    EntityConfig,
    entity_config_from_entry,
    is_configured,
)
from custom_components.discrete_statistics.const import (
    DEFAULT_IGNORE_SHORT,
    DEFAULT_RECORD,
    DEFAULT_RECORD_KNOWN,
    DOMAIN,
)


def parse(entities):
    return CONFIG_SCHEMA({DOMAIN: entities})[DOMAIN]


def test_minimal_config():
    [cfg] = parse([{"entity_id": "binary_sensor.grid_status"}])
    assert cfg.entity_id == "binary_sensor.grid_status"
    assert cfg.name is None
    assert cfg.default == "record_known"
    assert cfg.states == {}


def test_record_known_ignores_unknown_and_unavailable():
    [cfg] = parse([{"entity_id": "binary_sensor.grid_status"}])
    assert cfg.resolve("on") == "on"
    assert cfg.resolve("off") == "off"
    assert cfg.resolve("unknown") is None
    assert cfg.resolve("unavailable") is None


def test_default_record_keeps_everything():
    [cfg] = parse(
        [{"entity_id": "binary_sensor.x", "default": "record"}]
    )
    assert cfg.resolve("unknown") == "unknown"
    assert cfg.resolve("anything") == "anything"


def test_default_ignore_drops_unlisted():
    [cfg] = parse(
        [
            {
                "entity_id": "sensor.hvac",
                "default": "ignore",
                "states": {"heating": "record", "cooling": "record"},
            }
        ]
    )
    assert cfg.resolve("heating") == "heating"
    assert cfg.resolve("cooling") == "cooling"
    assert cfg.resolve("idle") is None


def test_explicit_ignore_overrides_default_record():
    [cfg] = parse(
        [
            {
                "entity_id": "sensor.x",
                "default": "record",
                "states": {"unavailable": "ignore"},
            }
        ]
    )
    assert cfg.resolve("unavailable") is None


def test_explicit_record_opts_back_in_under_record_known():
    [cfg] = parse(
        [{"entity_id": "sensor.x", "states": {"unknown": "record"}}]
    )
    assert cfg.resolve("unknown") == "unknown"


def test_mapping():
    [cfg] = parse([{"entity_id": "sensor.x", "states": {"cool": "cooling"}}])
    assert cfg.resolve("cool") == "cooling"


def test_mapping_does_not_chain():
    [cfg] = parse(
        [
            {
                "entity_id": "sensor.x",
                "states": {"a": "b", "b": "c"},
            }
        ]
    )
    assert cfg.resolve("a") == "b"


def test_map_target_is_recorded_even_under_default_ignore():
    [cfg] = parse(
        [
            {
                "entity_id": "sensor.x",
                "default": "ignore",
                "states": {"cool": "cooling"},
            }
        ]
    )
    assert cfg.resolve("cool") == "cooling"
    assert cfg.resolve("cooling") == "cooling"


def test_invalid_default_rejected():
    with pytest.raises(vol.Invalid):
        parse([{"entity_id": "sensor.x", "default": "nonsense"}])


def test_entity_id_required():
    with pytest.raises(vol.Invalid):
        parse([{"name": "no entity"}])


def test_name_is_carried_through():
    [cfg] = parse(
        [{"entity_id": "sensor.x", "name": "Grid Status"}]
    )
    assert cfg.name == "Grid Status"


def test_disposition_keyword_is_not_treated_as_a_map_target():
    [cfg] = parse(
        [
            {
                "entity_id": "sensor.x",
                "default": "ignore",
                "states": {"x": "ignore"},
            }
        ]
    )
    assert cfg.resolve("ignore") is None


def test_record_keyword_is_not_treated_as_a_map_target():
    [cfg] = parse(
        [
            {
                "entity_id": "sensor.x",
                "default": "ignore",
                "states": {"x": "record"},
            }
        ]
    )
    assert cfg.resolve("record") is None


def test_duplicate_entity_id_is_rejected():
    """Two configs for one entity write conflicting values to the same IDs."""
    with pytest.raises(vol.Invalid, match="configured more than once"):
        parse(
            [
                {"entity_id": "binary_sensor.grid_status"},
                {
                    "entity_id": "binary_sensor.grid_status",
                    "default": "ignore",
                    "states": {"on": "record"},
                },
            ]
        )


def test_distinct_entity_ids_are_accepted():
    configs = parse(
        [
            {"entity_id": "binary_sensor.grid_status"},
            {"entity_id": "binary_sensor.other"},
        ]
    )
    assert [cfg.entity_id for cfg in configs] == [
        "binary_sensor.grid_status",
        "binary_sensor.other",
    ]


def test_is_configured_matches_on_entity_id():
    configs = [
        EntityConfig(
            entity_id="binary_sensor.a", name=None, default=DEFAULT_RECORD
        )
    ]
    assert is_configured(configs, "binary_sensor.a")
    assert not is_configured(configs, "binary_sensor.b")
    assert not is_configured([], "binary_sensor.a")


def test_entity_config_from_entry_reads_data_and_options():
    cfg = entity_config_from_entry(
        {CONF_ENTITY_ID: "binary_sensor.a"},
        {CONF_NAME: "Grid", CONF_DEFAULT: DEFAULT_RECORD},
    )
    assert cfg == EntityConfig(
        entity_id="binary_sensor.a", name="Grid", default=DEFAULT_RECORD
    )


def test_entity_config_from_entry_defaults_and_blank_name():
    cfg = entity_config_from_entry({CONF_ENTITY_ID: "binary_sensor.a"}, {})
    assert cfg.default == DEFAULT_RECORD_KNOWN
    assert cfg.name is None

    # An empty text field arrives as "" and must not become the display name.
    blank = entity_config_from_entry(
        {CONF_ENTITY_ID: "binary_sensor.a"}, {CONF_NAME: ""}
    )
    assert blank.name is None


def test_entity_config_from_entry_has_no_state_map():
    # The v1 UI offers no per-state mapping; resolve() must fall through
    # to the default for every state.
    cfg = entity_config_from_entry({CONF_ENTITY_ID: "binary_sensor.a"}, {})
    assert cfg.states == {}


def test_an_empty_state_is_treated_as_unknown():
    """The recorder stores NULL when an entity is removed or reloaded.

    It cannot go in a statistic ID, and it is not an absence of data - it is
    a state we cannot name. So it inherits whatever disposition `unknown`
    has, rather than getting a rule of its own: substituted and then
    resolved, not answered directly, or `default` would be bypassed.
    """
    known = parse([{"entity_id": "sensor.x", "default": "record_known"}])[0]
    assert known.resolve("") is None

    recorded = parse([{"entity_id": "sensor.x", "default": "record"}])[0]
    assert recorded.resolve("") == "unknown"


def test_an_unsluggable_state_is_treated_as_unknown():
    recorded = parse([{"entity_id": "sensor.x", "default": "record"}])[0]
    assert recorded.resolve("!!!") == "unknown"
    assert recorded.resolve("-") == "unknown"
    assert recorded.resolve("unknown") == "unknown"

    known = parse([{"entity_id": "sensor.x", "default": "record_known"}])[0]
    assert known.resolve("!!!") is None


def test_the_conversion_follows_a_states_map_for_unknown():
    """Because it happens before resolution, not after it."""
    cfg = parse(
        [{"entity_id": "sensor.x", "states": {"unknown": "offline"}}]
    )[0]
    assert cfg.resolve("") == "offline"
    assert cfg.resolve("!!!") == "offline"


def test_a_map_target_that_cannot_form_an_id_is_rejected():
    with pytest.raises(vol.Invalid, match="usable statistic ID"):
        parse([{"entity_id": "sensor.x", "states": {"weird": "!!!"}}])


def test_an_ignored_state_stays_ignored():
    """Ignoring means carry-forward."""
    cfg = parse([{"entity_id": "sensor.x", "states": {"blip": "ignore"}}])[0]
    assert cfg.resolve("blip") is None


def test_the_blank_option_catches_what_has_no_name():
    cfg = parse([{"entity_id": "sensor.x", "blank": "ok"}])[0]
    assert cfg.resolve("") == "ok"
    assert cfg.resolve("!!!") == "ok"
    # A real unknown is untouched by it.
    assert cfg.resolve("unknown") is None


def test_the_two_compose_with_the_explicit_entry_first():
    """Blank is the most important state a text error sensor has.

    An error sensor reports "" for "no error", so it must be mappable. The
    substitution is a fallback for states nobody has named, not an override
    of the config.
    """
    cfg = parse(
        [{
            "entity_id": "sensor.x",
            "blank": "weird",
            "states": {"": "ok"},
        }]
    )[0]
    assert cfg.resolve("") == "ok"
    assert cfg.resolve("!!!") == "weird"


def test_the_blank_default_is_unknown():
    cfg = parse([{"entity_id": "sensor.x"}])[0]
    assert cfg.blank == "unknown"


def test_an_unusable_blank_value_is_rejected():
    for bad in ("", "!!!"):
        with pytest.raises(vol.Invalid):
            parse([{"entity_id": "sensor.x", "blank": bad}])


def test_an_explicitly_recorded_blank_falls_back_rather_than_crashing():
    """`"": record` names it but it still cannot become an ID."""
    cfg = parse(
        [{
            "entity_id": "sensor.x",
            "default": "record",
            "states": {"": "record"},
        }]
    )[0]
    assert cfg.resolve("") == "unknown"


def test_a_state_spelling_unknown_resolves_to_unknown():
    """It has a name, so it is not routed through `blank:`."""
    cfg = parse([{"entity_id": "sensor.x", "blank": "ok", "default": "record"}])[0]
    assert cfg.resolve("__unknown__") == "__unknown__"
    assert cfg.resolve("!!!") == "ok"


def test_blank_can_be_ignored_outright():
    """Without conflating blanks with genuine unknowns.

    `blank: unknown` plus `states: {unknown: ignore}` would reach the same
    result for blanks, but would silently ignore real unknowns too.
    """
    cfg = parse([{"entity_id": "sensor.x", "default": "record", "blank": "ignore"}])[0]
    assert cfg.resolve("") is None
    assert cfg.resolve("!!!") is None
    assert cfg.resolve("🙂") is None
    # A real unknown is untouched by it.
    assert cfg.resolve("unknown") == "unknown"


def test_blank_ignore_still_loses_to_an_explicit_mapping():
    cfg = parse(
        [{"entity_id": "sensor.x", "blank": "ignore", "states": {"": "ok"}}]
    )[0]
    assert cfg.resolve("") == "ok"
    assert cfg.resolve("!!!") is None


def test_record_is_not_a_valid_blank_setting():
    """There is no name to record it under - that is the point of the option."""
    with pytest.raises(vol.Invalid, match="no name to record"):
        parse([{"entity_id": "sensor.x", "blank": "record"}])


def test_ignore_short_resolves_to_the_state_and_flags_it():
    """Whether a spell is long enough is a question about a spell, not a
    state, so `resolve` records it and `classify` says it is conditional."""
    cfg = parse(
        [{
            "entity_id": "sensor.x",
            "states": {"unavailable": "ignore_short"},
            "min_duration": {"seconds": 30},
        }]
    )[0]
    assert cfg.min_duration == 30.0
    assert cfg.resolve("unavailable") == "unavailable"
    assert cfg.classify("unavailable") == ("unavailable", True)
    assert cfg.classify("on") == ("on", False)
    assert cfg.classify("unknown") == (None, False)


def test_ignore_short_as_the_default_flags_every_unlisted_state():
    cfg = parse(
        [{
            "entity_id": "binary_sensor.door",
            "default": "ignore_short",
            "states": {"unavailable": "ignore"},
            "min_duration": "00:00:05",
        }]
    )[0]
    assert cfg.min_duration == 5.0
    assert cfg.classify("on") == ("on", True)
    assert cfg.classify("off") == ("off", True)
    assert cfg.classify("unavailable") == (None, False)


def test_a_blank_state_inherits_ignore_short_from_its_substitute():
    cfg = parse(
        [{
            "entity_id": "sensor.x",
            "states": {"unknown": "ignore_short"},
            "min_duration": {"minutes": 1},
        }]
    )[0]
    assert cfg.classify("") == ("unknown", True)
    assert cfg.classify("!!!") == ("unknown", True)


def test_ignore_short_is_not_a_map_target():
    cfg = parse(
        [{
            "entity_id": "sensor.x",
            "default": "ignore",
            "states": {"x": "ignore_short"},
            "min_duration": {"minutes": 1},
        }]
    )[0]
    assert cfg.resolve("ignore_short") is None


def test_ignore_short_requires_a_threshold():
    """Without one it would be plain `record`."""
    with pytest.raises(vol.Invalid, match="min_duration"):
        parse([{"entity_id": "sensor.x", "default": "ignore_short"}])
    with pytest.raises(vol.Invalid, match="min_duration"):
        parse(
            [{"entity_id": "sensor.x", "states": {"unknown": "ignore_short"}}]
        )
    with pytest.raises(vol.Invalid, match="min_duration"):
        parse(
            [{
                "entity_id": "sensor.x",
                "default": "ignore_short",
                "min_duration": {"seconds": 0},
            }]
        )


def test_the_threshold_is_capped_at_an_hour():
    """The distance the compiler reads back before a window."""
    parse(
        [{
            "entity_id": "sensor.x",
            "default": "ignore_short",
            "min_duration": {"hours": 1},
        }]
    )
    with pytest.raises(vol.Invalid, match="one hour"):
        parse(
            [{
                "entity_id": "sensor.x",
                "default": "ignore_short",
                "min_duration": {"hours": 1, "seconds": 1},
            }]
        )


def test_a_threshold_without_ignore_short_is_inert_and_never_rejected():
    """Nothing reads it, so nothing is wrong with it - whatever it says."""
    [cfg] = parse([{"entity_id": "sensor.x", "min_duration": {"minutes": 1}}])
    assert cfg.min_duration == 60.0
    [cfg] = parse([{"entity_id": "sensor.x", "min_duration": {"seconds": 0}}])
    assert cfg.min_duration == 0.0
    [cfg] = parse([{"entity_id": "sensor.x", "min_duration": {"hours": 2}}])
    assert cfg.min_duration == 7200.0
    [cfg] = parse([{"entity_id": "sensor.x"}])
    assert cfg.min_duration == 0.0


def test_entity_config_from_entry_reads_the_minimum_duration():
    cfg = entity_config_from_entry(
        {CONF_ENTITY_ID: "binary_sensor.a"},
        {CONF_DEFAULT: DEFAULT_IGNORE_SHORT, CONF_MIN_DURATION: 20.0},
    )
    assert cfg.min_duration == 20.0
    assert cfg.classify("on") == ("on", True)
    assert entity_config_from_entry({CONF_ENTITY_ID: "binary_sensor.a"}, {}).min_duration == 0.0


def test_ignore_short_unknown_is_conditional_only_for_unavailable_and_unknown():
    cfg = parse(
        [{
            "entity_id": "sensor.x",
            "default": "ignore_short_unknown",
            "min_duration": {"minutes": 1},
        }]
    )[0]
    assert cfg.classify("on") == ("on", False)
    assert cfg.classify("unavailable") == ("unavailable", True)
    assert cfg.classify("unknown") == ("unknown", True)
    assert cfg.classify("") == ("unknown", True)


def test_ignore_short_unknown_requires_a_threshold_too():
    with pytest.raises(vol.Invalid, match="min_duration"):
        parse([{"entity_id": "sensor.x", "default": "ignore_short_unknown"}])
