"""Tests for configuration parsing and state dispositions."""

import pytest
import voluptuous as vol

from custom_components.discrete_stats.config import CONFIG_SCHEMA, EntityConfig
from custom_components.discrete_stats.const import DOMAIN


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


def test_no_data_rejected_as_a_states_key():
    with pytest.raises(vol.Invalid, match="no_data"):
        parse([{"entity_id": "sensor.x", "states": {"no_data": "record"}}])


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
