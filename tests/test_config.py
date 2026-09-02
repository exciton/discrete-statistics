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


def test_entity_config_is_hashable():
    cfg = EntityConfig(
        entity_id="sensor.x", name=None, default="record_known", states={}
    )
    assert isinstance(hash(cfg), int)


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
