"""A button that recompiles one entity's whole history.

Recompute has always been a service, which is fine for automations and
awkward for the thing people actually want: this entity's statistics look
wrong, rebuild them. The button also gives the config entry an entity of its
own, which is what stops the Helpers list drawing a red exclamation beside
every helper.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the recompute button for one tracked entity.

    The signature is Home Assistant's; `hass` is reached through the entity.
    """
    async_add_entities([RecomputeButton(entry)])


class RecomputeButton(ButtonEntity):
    """Rebuild this entity's statistics from the recorder's history."""

    _attr_has_entity_name = True
    _attr_translation_key = "recompute"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry) -> None:
        """Bind the button to its config entry."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_recompute"
        # A service device standing for the helper itself. Without one,
        # `has_entity_name` leaves the button called just "Recompute" -
        # every helper's button identical, and `button.recompute` taken by
        # whichever was created first.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        """Start a full recompute, without holding up the press.

        A backfill can run for minutes and takes the shared lock, so this
        schedules it as a background task tied to the entry - the same route
        a configuration change uses - and lets the notification report the
        outcome.
        """
        from . import async_recompute_entry  # noqa: PLC0415 - avoids a cycle

        self._entry.async_create_background_task(
            self.hass,
            async_recompute_entry(self.hass, self._entry),
            name=f"{DOMAIN} recompute button {self._entry.entry_id}",
        )
