"""Configuration schema and state disposition resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import voluptuous as vol
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME, STATE_UNKNOWN
from homeassistant.helpers import config_validation as cv

from .statistic_ids import is_blank, state_token
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

_NO_DATA_TOKEN = state_token(NO_DATA)

CONF_DEFAULT = "default"
CONF_STATES = "states"
CONF_BLANK = "blank"

DEFAULTS = (DEFAULT_RECORD, DEFAULT_RECORD_KNOWN, DEFAULT_IGNORE)


@dataclass(frozen=True)
class EntityConfig:
    """Resolved configuration for one tracked entity."""

    entity_id: str
    name: str | None
    default: str
    states: Mapping[str, str] = field(default_factory=dict)
    # What a state with no name of its own becomes: `ignore` to carry the
    # previous state forward, or a state name to be substituted before the
    # default is applied - so the stock `unknown` is ignored by
    # `record_known` exactly as a real `unknown` would be.
    blank: str = STATE_UNKNOWN

    def resolve(self, raw_state: str) -> str | None:
        """Return the canonical state for a raw state, or None to ignore it.

        None means carry-forward: the previous canonical state continues and
        no transition is counted.

        A blank state - no letters or digits, which is what the recorder
        stores as NULL when an entity is removed or reloaded - is handled by
        `blank:`. That either ignores it outright, or substitutes a name
        before anything else looks at it, so it inherits a real state's
        disposition rather than needing a rule of its own.

        An explicit entry in `states` wins over that substitution. Blank can
        carry real meaning: for a text sensor reporting an error, it is
        usually the most important state there is, and only the config knows
        that.

        NO_DATA may be reached only on purpose. A device that happens to
        report the string `no_data` must not slip into the band reserved for
        "we could not tell", because that would make it and "the device said
        this" the same reading; but an operator who writes `no_data` as a
        target is asking for exactly that, and is allowed it. The test is who
        asked, not what the value is.
        """
        chosen = raw_state in self.states
        if not chosen and is_blank(raw_state):
            if self.blank == DISPOSITION_IGNORE:
                return None
            # The operator picked the substitute, so this counts as chosen.
            raw_state = self.blank
            chosen = True

        canonical = self._resolve(raw_state)
        if canonical is None:
            return None
        if not chosen and state_token(canonical) == _NO_DATA_TOKEN:
            return None
        if is_blank(canonical):
            # Named explicitly - `"": record` - but still not an ID.
            canonical = self._resolve(self.blank)
        return canonical

    def _resolve(self, raw_state: str) -> str | None:
        """Apply the disposition table and the default, without the guard."""
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


def blank_error(value: str) -> str | None:
    """Return a translation key when `blank:` cannot take this, else None.

    `ignore` carries the previous state forward. Any other value is a state
    name to substitute. `record` is refused rather than quietly treated as a
    name: a blank state has nothing to record it under, which is the whole
    reason this setting exists.

    Shared with the config flow, so YAML and the UI cannot disagree about
    what is acceptable.
    """
    if value == DISPOSITION_IGNORE:
        return None
    if value == DISPOSITION_RECORD:
        return "blank_record"
    if is_blank(value):
        return "blank_unusable"
    return None


def _usable_blank(value: str) -> str:
    """Validate the `blank:` setting for YAML."""
    if (problem := blank_error(value)) == "blank_record":
        raise vol.Invalid(
            f"{DISPOSITION_RECORD!r} is not a valid {CONF_BLANK!r} setting: "
            f"a blank state has no name to record it under"
        )
    if problem is not None:
        raise vol.Invalid(f"{value!r} does not produce a usable statistic ID")
    return value


def _usable_state_name(value: str) -> str:
    """Reject a name that cannot itself become a statistic.

    NO_DATA is allowed: naming it is how an operator says "I cannot
    interpret this state, chart it as a gap". What stays forbidden is a
    device reaching the band on its own, which `resolve` enforces.
    """
    if state_token(value) == _NO_DATA_TOKEN:
        return value
    if is_blank(value):
        raise vol.Invalid(
            f"{value!r} does not produce a usable statistic ID"
        )
    return value


def _usable_map_targets(states: dict[str, str]) -> dict[str, str]:
    """Reject map targets that cannot become a statistic.

    Only a target becomes a canonical state, and only those reach an ID.
    Keys are raw recorder values and stay unrestricted, or an entity that
    genuinely reports `No Data` could not be mapped anywhere.
    """
    for value in states.values():
        if value not in (DISPOSITION_RECORD, DISPOSITION_IGNORE):
            _usable_state_name(value)
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
        vol.Optional(CONF_BLANK, default=STATE_UNKNOWN): vol.All(
            cv.string, _usable_blank
        ),
        vol.Optional(CONF_STATES, default=dict): vol.All(
            {cv.string: cv.string}, _usable_map_targets
        ),
    }
)


def _to_entity_config(raw: dict[str, Any]) -> EntityConfig:
    return EntityConfig(
        entity_id=raw[CONF_ENTITY_ID],
        name=raw.get(CONF_NAME),
        default=raw[CONF_DEFAULT],
        states=raw[CONF_STATES],
        blank=raw[CONF_BLANK],
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
        # Not in the options flow yet; reading it here keeps the two config
        # sources symmetric so adding the field is only a form change.
        blank=options.get(CONF_BLANK) or STATE_UNKNOWN,
    )
