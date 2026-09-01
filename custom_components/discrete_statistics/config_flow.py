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
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .config import CONF_DEFAULT, is_configured
from .const import DEFAULT_RECORD, DEFAULT_RECORD_KNOWN, DOMAIN

# `ignore` is deliberately absent. With no per-state mapping to supply
# exceptions it makes resolve() return None for every state, so nothing is
# ever recordable and the entity's whole timeline is attributed to no_data.
# It stays valid in YAML, where `states:` supplies those exceptions.
UI_DEFAULTS = [DEFAULT_RECORD, DEFAULT_RECORD_KNOWN]

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
    }
)

USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_ENTITY_ID): selector.EntitySelector()}
).extend(OPTIONS_SCHEMA.schema)


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
            title=name or entity_id,
            data={CONF_ENTITY_ID: entity_id},
            options={CONF_NAME: name, CONF_DEFAULT: user_input[CONF_DEFAULT]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
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
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_NAME: user_input.get(CONF_NAME) or None,
                    CONF_DEFAULT: user_input[CONF_DEFAULT],
                }
            )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
