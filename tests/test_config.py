"""Tests for configuration parsing and state dispositions."""

import pytest
import voluptuous as vol

from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from custom_components.discrete_statistics.config import (
    CONF_DEFAULT,
    CONFIG_SCHEMA,
    EntityConfig,
    entity_config_from_entry,
    is_configured,
)
from custom_components.discrete_statistics.const import (
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


def test_source_state_no_data_is_ignored_at_runtime():
    [cfg] = parse([{"entity_id": "sensor.x", "default": "record"}])
    assert cfg.resolve("no_data") is None


def test_no_data_is_allowed_as_a_states_key_but_not_recorded_as_itself():
    """A key is a raw source state and never becomes a statistic ID.

    Recording it under its own name would collide with the compiler's
    reserved band, so `record` still resolves to nothing - but the key
    itself is accepted, which is what makes mapping it away possible.
    """
    cfg = parse([{"entity_id": "sensor.x", "states": {"no_data": "record"}}])[0]
    assert cfg.resolve("no_data") is None


def test_no_data_rejected_as_a_map_target():
    with pytest.raises(vol.Invalid, match="no_data"):
        parse([{"entity_id": "sensor.x", "states": {"weird": "no_data"}}])


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


def test_genuine_map_target_still_forced_under_default_ignore():
    [cfg] = parse(
        [
            {
                "entity_id": "sensor.x",
                "default": "ignore",
                "states": {"cool": "cooling"},
            }
        ]
    )
    assert cfg.resolve("cooling") == "cooling"


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


def test_a_state_that_tokenises_to_no_data_can_be_mapped_away():
    """The reserved band must not be a dead end.

    A raw `No Data` would reach the compiler's own statistic ID if recorded
    under its own name, so it is ignored by default - but mapping it
    elsewhere is safe and must be allowed, because otherwise an entity that
    reports it has no way to record that state at all.
    """
    cfg = parse([{"entity_id": "sensor.x", "states": {"No Data": "missing"}}])[0]
    assert cfg.resolve("No Data") == "missing"


def test_an_unmapped_state_tokenising_to_no_data_is_still_ignored():
    cfg = parse([{"entity_id": "sensor.x", "default": "record"}])[0]
    assert cfg.resolve("No Data") is None
    assert cfg.resolve("no_data") is None
    assert cfg.resolve("nodata") is None


def test_a_map_target_tokenising_to_no_data_is_rejected():
    with pytest.raises(vol.Invalid, match="no_data"):
        parse([{"entity_id": "sensor.x", "states": {"weird": "No Data"}}])


def test_an_empty_state_is_treated_as_unknown():
    """The recorder stores NULL when an entity is removed or reloaded.

    It cannot go in a statistic ID, and it is not an absence of data - it is
    a state we cannot name. So it inherits whatever disposition `unknown`
    has, rather than getting a rule of its own.
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


def test_an_explicit_mapping_beats_the_unrepresentable_substitution():
    """Blank is the most important state a text error sensor has.

    An error sensor reports "" for "no error", so it must be mappable. The
    substitution is a fallback for states nobody has named, not an override
    of the config.
    """
    cfg = parse([{"entity_id": "sensor.x", "states": {"": "ok"}}])[0]
    assert cfg.resolve("") == "ok"


def test_the_unrepresentable_option_catches_what_is_not_named():
    cfg = parse([{"entity_id": "sensor.x", "unrepresentable": "ok"}])[0]
    assert cfg.resolve("") == "ok"
    assert cfg.resolve("!!!") == "ok"
    # A real unknown is untouched by it.
    assert cfg.resolve("unknown") is None


def test_the_two_compose_with_the_explicit_entry_first():
    cfg = parse(
        [{
            "entity_id": "sensor.x",
            "unrepresentable": "weird",
            "states": {"": "ok"},
        }]
    )[0]
    assert cfg.resolve("") == "ok"
    assert cfg.resolve("!!!") == "weird"


def test_the_substitution_still_runs_through_the_default():
    """Not a direct answer, or it would override `default` silently.

    This is what keeps `unrepresentable: unknown` a genuine no-op: it must
    still be ignored by record_known exactly as a real unknown would be.
    """
    known = parse([{"entity_id": "sensor.x", "default": "record_known"}])[0]
    assert known.resolve("") is None
    recorded = parse([{"entity_id": "sensor.x", "default": "record"}])[0]
    assert recorded.resolve("") == "unknown"

    # And a substitute that is not an ignored state is recorded either way.
    both = parse(
        [{"entity_id": "sensor.x", "unrepresentable": "ok"}]
    )[0]
    assert both.resolve("") == "ok"


def test_the_unrepresentable_default_is_unchanged_behaviour():
    cfg = parse([{"entity_id": "sensor.x"}])[0]
    assert cfg.unrepresentable == "unknown"


def test_an_unusable_unrepresentable_value_is_rejected():
    for bad in ("", "!!!", "no_data"):
        with pytest.raises(vol.Invalid):
            parse([{"entity_id": "sensor.x", "unrepresentable": bad}])


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
