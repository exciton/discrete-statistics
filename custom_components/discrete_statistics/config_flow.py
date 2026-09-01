"""Config flow for discrete_statistics.

Hand-written rather than SchemaConfigFlowHandler: the YAML-clash guard and
the fixed entity_id are both awkward to express through the schema helper,
and it is one fewer Home Assistant API whose signature moves between
releases.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
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

        data = self.hass.data.get(DOMAIN)
        if data is not None and is_configured(data["yaml_configs"], entity_id):
            return self.async_abort(reason="yaml_configured")

        name = user_input.get(CONF_NAME) or None
        return self.async_create_entry(
            title=name or entity_id,
            data={CONF_ENTITY_ID: entity_id},
            options={CONF_NAME: name, CONF_DEFAULT: user_input[CONF_DEFAULT]},
        )
