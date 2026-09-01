"""Configuration schema and state disposition resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import voluptuous as vol
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.helpers import config_validation as cv

from .const import (
    DEFAULT_IGNORE,
    DEFAULT_RECORD,
    DEFAULT_RECORD_KNOWN,
    DISPOSITION_IGNORE,
    DISPOSITION_RECORD,
    DOMAIN,
    NO_DATA,
    UNKNOWN_STATES,
)

CONF_DEFAULT = "default"
CONF_STATES = "states"

DEFAULTS = (DEFAULT_RECORD, DEFAULT_RECORD_KNOWN, DEFAULT_IGNORE)


@dataclass(frozen=True)
class EntityConfig:
    """Resolved configuration for one tracked entity."""

    entity_id: str
    name: str | None
    default: str
    states: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Freeze the mapping so the dataclass is genuinely immutable and
        # hashable, which lets configs be used as dict keys and set members.
        object.__setattr__(self, "states", MappingProxyType(dict(self.states)))

    def __hash__(self) -> int:
        return hash(
            (self.entity_id, self.name, self.default, tuple(sorted(self.states.items())))
        )

    def resolve(self, raw_state: str) -> str | None:
        """Return the canonical state for a raw state, or None to ignore it.

        None means carry-forward: the previous canonical state continues and
        no transition is counted.
        """
        # NO_DATA is reserved for the compiler. A source entity reporting it
        # is ignored rather than allowed to merge with the reserved band.
        if raw_state == NO_DATA:
            return None

        if (disposition := self.states.get(raw_state)) is not None:
            if disposition == DISPOSITION_IGNORE:
                return None
            if disposition == DISPOSITION_RECORD:
                return raw_state
            return disposition  # a map target

        # A map target is always recorded, whatever the default says. The
        # disposition keywords share the value slot with map targets, so they
        # must be excluded — otherwise a raw state literally named "record" or
        # "ignore" would be force-recorded because some unrelated key used that
        # keyword.
        map_targets = {
            value
            for value in self.states.values()
            if value not in (DISPOSITION_RECORD, DISPOSITION_IGNORE)
        }
        if raw_state in map_targets:
            return raw_state

        if self.default == DEFAULT_RECORD:
            return raw_state
        if self.default == DEFAULT_RECORD_KNOWN:
            return None if raw_state in UNKNOWN_STATES else raw_state
        return None  # DEFAULT_IGNORE


def _no_reserved_state(states: dict[str, str]) -> dict[str, str]:
    """Reject the reserved NO_DATA name as a key or a map target."""
    for key, value in states.items():
        if key == NO_DATA:
            raise vol.Invalid(
                f"{NO_DATA!r} is reserved and cannot be used as a state key"
            )
        if value == NO_DATA:
            raise vol.Invalid(
                f"{NO_DATA!r} is reserved and cannot be used as a map target"
            )
    return states


def _no_duplicate_entities(configs: list[EntityConfig]) -> list[EntityConfig]:
    """Reject an entity configured more than once.

    Two configurations for one entity resolve the same raw states through
    different disposition tables and write conflicting values to the same
    statistic IDs.
    """
    seen: set[str] = set()
    for cfg in configs:
        if cfg.entity_id in seen:
            raise vol.Invalid(
                f"{cfg.entity_id} is configured more than once; "
                f"{DOMAIN} allows one configuration per entity"
            )
        seen.add(cfg.entity_id)
    return configs


ENTITY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_NAME): vol.Any(str, None),
        vol.Optional(CONF_DEFAULT, default=DEFAULT_RECORD_KNOWN): vol.In(DEFAULTS),
        vol.Optional(CONF_STATES, default=dict): vol.All(
            {cv.string: cv.string}, _no_reserved_state
        ),
    }
)


def _to_entity_config(raw: dict[str, Any]) -> EntityConfig:
    return EntityConfig(
        entity_id=raw[CONF_ENTITY_ID],
        name=raw.get(CONF_NAME),
        default=raw[CONF_DEFAULT],
        states=raw[CONF_STATES],
    )


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.ensure_list,
            [vol.All(ENTITY_SCHEMA, _to_entity_config)],
            _no_duplicate_entities,
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def is_configured(configs: Iterable[EntityConfig], entity_id: str) -> bool:
    """Whether an entity already has a configuration in `configs`."""
    return any(cfg.entity_id == entity_id for cfg in configs)


def entity_config_from_entry(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> EntityConfig:
    """Build an EntityConfig from a config entry's data and options.

    Takes mappings rather than a ConfigEntry so this module stays pure and
    testable without Home Assistant. `entity_id` lives in data because it is
    identity: it builds every statistic ID and is never editable. An empty
    name arrives from the text field as "" and is normalised to None, which
    payload renders as the entity ID.
    """
    return EntityConfig(
        entity_id=data[CONF_ENTITY_ID],
        name=options.get(CONF_NAME) or None,
        default=options.get(CONF_DEFAULT, DEFAULT_RECORD_KNOWN),
    )
