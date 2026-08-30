"""Configuration schema and state disposition resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

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

        # A map target is always recorded, whatever the default says.
        if raw_state in self.states.values():
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
        )
    },
    extra=vol.ALLOW_EXTRA,
)
