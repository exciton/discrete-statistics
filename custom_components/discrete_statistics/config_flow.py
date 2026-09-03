"""Config flow for discrete_statistics.

Hand-written rather than SchemaConfigFlowHandler: the YAML-clash guard and
the fixed entity_id are both awkward to express through the schema helper,
and it is one fewer Home Assistant API whose signature moves between
releases.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_ENTITY_ID,
    CONF_NAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .config import CONF_BLANK, CONF_DEFAULT, blank_error, is_configured
from .naming import describe, display_name
from .const import (
    DEFAULT_RECORD,
    DEFAULT_RECORD_KNOWN,
    DISPOSITION_IGNORE,
    DOMAIN,
    NO_DATA,
)
from homeassistant.const import STATE_UNKNOWN

# `ignore` is deliberately absent. With no per-state mapping to supply
# exceptions it makes resolve() return None for every state, so nothing is
# ever recordable and the entity's whole timeline is attributed to no_data.
# It stays valid in YAML, where `states:` supplies those exceptions.
UI_DEFAULTS = [DEFAULT_RECORD, DEFAULT_RECORD_KNOWN]

# Offered, not exhaustive: `blank` takes any state name, and mapping to a
# real one is the point for a text sensor whose blank means "no error".
# custom_value lets the dropdown be typed into.
BLANK_SUGGESTIONS = [STATE_UNKNOWN, DISPOSITION_IGNORE, NO_DATA]

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_DEFAULT, default=DEFAULT_RECORD_KNOWN): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=UI_DEFAULTS,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key=CONF_DEFAULT,
            )
        ),
        vol.Required(CONF_BLANK, default=STATE_UNKNOWN): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=BLANK_SUGGESTIONS,
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key=CONF_BLANK,
                custom_value=True,
            )
        ),
    }
)

USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_ENTITY_ID): selector.EntitySelector()}
).extend(OPTIONS_SCHEMA.schema)


# Not imported from homeassistant.components.sensor: that would make sensor a
# manifest dependency for one string.
ATTR_STATE_CLASS = "state_class"


def _has_continuous_state(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity measures rather than reports a set of states.

    `state_class` is the strongest signal - it is precisely what tells the
    recorder to build its own long-term statistics - and a unit is the next
    best: units are for numbers.

    Both the registry and the live state are consulted. The registry keeps
    working while an entity is unavailable, when attributes have been
    stripped; the live state covers entities that never registered, such as
    template sensors. Either saying yes is enough, and a false positive is
    not a risk in practice, because a genuinely discrete entity has neither.
    """
    if (entry := er.async_get(hass).async_get(entity_id)) is not None:
        if (entry.capabilities or {}).get(ATTR_STATE_CLASS):
            return True
        if entry.unit_of_measurement:
            return True
    if (state := hass.states.get(entity_id)) is not None:
        attributes = state.attributes
        if attributes.get(ATTR_STATE_CLASS) or attributes.get(
            ATTR_UNIT_OF_MEASUREMENT
        ):
            return True
    return False


class DiscreteStatisticsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one tracked entity."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an entity and how its states are recorded."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)

        entity_id = user_input[CONF_ENTITY_ID]

        errors: dict[str, str] = {}
        if problem := blank_error(user_input[CONF_BLANK]):
            errors[CONF_BLANK] = problem
        if _has_continuous_state(self.hass, entity_id):
            errors[CONF_ENTITY_ID] = "continuous_state"
        if errors:
            # A field error, not an abort: the dialog stays open so another
            # entity can be picked without starting again.
            return self.async_show_form(
                step_id="user",
                data_schema=self.add_suggested_values_to_schema(
                    USER_SCHEMA, user_input
                ),
                errors=errors,
            )

        # entity_id is identity: it builds every statistic ID, so it is the
        # unique id and is never editable afterwards.
        await self.async_set_unique_id(entity_id)
        self._abort_if_unique_id_configured()

        # On a fresh install with no YAML and no existing entries, the
        # component has never run: starting a flow only imports this module,
        # and async_setup runs when the first entry is created - so
        # hass.data[DOMAIN] genuinely does not exist yet. This guard is the
        # only thing standing between that state and a KeyError on the very
        # first helper anyone creates. Skipping the YAML check in that case
        # is also correct: with no async_setup having run, no YAML config
        # can exist to clash with.
        data = self.hass.data.get(DOMAIN)
        if data is not None and is_configured(data["yaml_configs"], entity_id):
            return self.async_abort(reason="yaml_configured")

        name = user_input.get(CONF_NAME) or None
        return self.async_create_entry(
            # The entity's own name, not its ID: it is what the Helpers
            # list shows and what people recognise.
            title=display_name(self.hass, entity_id, name),
            data={CONF_ENTITY_ID: entity_id},
            options={
                CONF_NAME: name,
                CONF_DEFAULT: user_input[CONF_DEFAULT],
                CONF_BLANK: user_input[CONF_BLANK],
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow. entity_id is not editable here."""
        return DiscreteStatisticsOptionsFlow()


class DiscreteStatisticsOptionsFlow(OptionsFlow):
    """Edit the name and the recording default.

    entity_id is absent by design: it builds every statistic ID, so
    changing it would orphan the entity's whole series.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the editable options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if problem := blank_error(user_input[CONF_BLANK]):
                errors[CONF_BLANK] = problem
            else:
                return self.async_create_entry(
                    data={
                        CONF_NAME: user_input.get(CONF_NAME) or None,
                        CONF_DEFAULT: user_input[CONF_DEFAULT],
                        CONF_BLANK: user_input[CONF_BLANK],
                    }
                )
        # Which entity this is about, and what leaving the name blank would
        # give. NOT prefilled into the box: a suggested value comes back on
        # submit, which would freeze the name instead of letting it follow
        # the entity.
        entity_id = self.config_entry.data[CONF_ENTITY_ID]
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, user_input or self.config_entry.options
            ),
            errors=errors,
            description_placeholders={
                "entity": describe(self.hass, entity_id),
                "default_name": display_name(self.hass, entity_id),
            },
        )
