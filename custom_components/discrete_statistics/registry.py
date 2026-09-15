"""Follow the entity registry.

A rename of an entity we record moves its statistics with it. One
listener on the registry, filtered to the entities configured, and
nothing per entry.
"""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .compiler import Compiler
from .const import DOMAIN
from .naming import describe

_LOGGER = logging.getLogger(__name__)


def _owned(hass: HomeAssistant, entity_id: str) -> bool:
    return any(cfg.entity_id == entity_id for cfg in hass.data[DOMAIN]["all_configs"]())


def _entry_for(hass: HomeAssistant, entity_id: str) -> ConfigEntry | None:
    """The config entry recording `entity_id`; None for a YAML entity."""
    for entry_id, cfg in hass.data[DOMAIN]["entry_configs"].items():
        if cfg.entity_id == entity_id:
            return hass.config_entries.async_get_entry(entry_id)
    return None


def _notify(hass: HomeAssistant, message: str, notification_id: str) -> None:
    persistent_notification.async_create(
        hass, message, title="Discrete Statistics", notification_id=notification_id
    )


async def async_follow(
    hass: HomeAssistant, old_entity_id: str, new_entity_id: str
) -> None:
    """Move the statistics of a renamed entity, then the entry that records it.

    In that order, under the compile lock: a compile that opened before
    the metadata rename committed would find no watermark under the new
    name and rebuild the retained history as a second series. The reload
    is what points the coordinator at the new entity and compiles under
    it; `_async_entry_updated` leaves an entity-ID change alone for that
    reason.
    """
    data = hass.data[DOMAIN]
    entry = _entry_for(hass, old_entity_id)
    if entry is None:
        _LOGGER.warning(
            "%s was renamed to %s but is configured in configuration.yaml; "
            "its statistics stay under %s until the YAML names the new entity",
            old_entity_id,
            new_entity_id,
            old_entity_id,
        )
        _notify(
            hass,
            f"{describe(hass, new_entity_id)} was renamed from {old_entity_id}, "
            "which is configured in configuration.yaml. Its statistics were not "
            f"moved: update the YAML to name {new_entity_id}.",
            f"{DOMAIN}_rename_{old_entity_id}",
        )
        return

    compiler: Compiler = data["compiler"]
    cfg = data["entry_configs"][entry.entry_id]
    try:
        async with data["lock"]:
            renamed = await compiler.async_rename(old_entity_id, new_entity_id)
            hass.config_entries.async_update_entry(
                entry,
                unique_id=new_entity_id,
                data={**entry.data, CONF_ENTITY_ID: new_entity_id},
            )
            await hass.config_entries.async_reload(entry.entry_id)
    except Exception as err:  # reported, not raised into the bus
        _LOGGER.exception(
            "Following the rename of %s to %s failed", old_entity_id, new_entity_id
        )
        _notify(
            hass,
            f"Could not move the statistics of {old_entity_id} to {new_entity_id}: {err}",
            f"{DOMAIN}_rename_{entry.entry_id}",
        )
        return

    message = (
        f"Moved {len(renamed.moved)} statistic(s) of "
        f"{describe(hass, new_entity_id, cfg.name)} from {old_entity_id} to "
        f"{new_entity_id}."
    )
    if renamed.collided:
        message += (
            f" {len(renamed.collided)} statistic(s) already existed under the new "
            f"name and were left under {old_entity_id}: "
            f"{', '.join(renamed.collided)}. Delete the ones under the new name "
            "and run recompute to merge."
        )
    _LOGGER.info("%s", message)
    _notify(hass, message, f"{DOMAIN}_rename_{entry.entry_id}")


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Install the listeners. Called once, from the integration's setup."""

    @callback
    def registry_filter(event_data: er.EventEntityRegistryUpdatedData) -> bool:
        return _owned(hass, event_data["entity_id"]) or _owned(
            hass, event_data.get("old_entity_id", "")
        )

    async def registry_updated(
        event: Event[er.EventEntityRegistryUpdatedData],
    ) -> None:
        data = event.data
        if data["action"] == "update" and "old_entity_id" in data:
            old, new = data["old_entity_id"], data["entity_id"]
            if _owned(hass, old):
                await async_follow(hass, old, new)

    # `hass.bus.async_listen`, never `async_track_entity_registry_updated_event`:
    # the recorder installs its own registry listener at setup and refuses
    # to if that helper has been used first, and ours can be set up before
    # it when configured in YAML.
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, registry_updated, event_filter=registry_filter
    )
