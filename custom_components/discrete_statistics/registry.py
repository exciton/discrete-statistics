"""Follow the entity registry.

A rename of an entity we record moves its statistics with it, a change
of its name recomposes the titles built from it, a move between devices
reloads the entry whose sensors follow it, and an entity that disappears
raises a repair issue. One listener on the registry, filtered to the
entities configured, and nothing per entry.
"""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME, EVENT_STATE_CHANGED
from homeassistant.core import (
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.start import async_at_started

from .bucketer import hour_start
from .compiler import Compiler
from .config import CONF_FILLED_UNTIL
from .const import DOMAIN, SUBENTRY_SENSOR, SUBENTRY_STATE
from .naming import describe, sensor_title, state_title
from .reading import spec_from

_LOGGER = logging.getLogger(__name__)

# The filter runs on every registry event, so the rest are dropped before
# a handler is woken.
FOLLOWED_ACTIONS = ("create", "update", "remove")

# Either half of what `display_name` resolves an entity's name from.
_NAMES = frozenset({"name", "original_name"})


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

    A failure between the two leaves the statistics moved under a config
    that still names the old ID, and a second rename arriving mid-follow
    is dropped: both are reported, neither repaired.
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
            # Returns False rather than raising when the setup fails, so
            # the outcome has to be read, not caught.
            reloaded = await hass.config_entries.async_reload(entry.entry_id)
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
    if not reloaded:
        message += (
            f" The entry did not reload and is not recording {new_entity_id}: "
            "check the logs and reload it from Settings > Devices & Services."
        )
    _LOGGER.info("%s", message)
    _notify(hass, message, f"{DOMAIN}_rename_{entry.entry_id}")


async def async_fill(
    hass: HomeAssistant, entry: ConfigEntry, source_entity_id: str, before: float
) -> None:
    """Compile a replacement's early history into the series it was renamed onto."""
    data = hass.data[DOMAIN]
    compiler: Compiler = data["compiler"]
    cfg = data["entry_configs"][entry.entry_id]
    try:
        async with data["lock"]:
            filled = await compiler.async_fill(cfg, source_entity_id, before)
            # Only hours read under the source ID need the floor: hours
            # read under our own are rebuildable from our own history.
            if filled.hours and filled.read_from != cfg.entity_id:
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_FILLED_UNTIL: hour_start(before)},
                )
    except Exception as err:  # reported, not raised into the bus
        _LOGGER.exception("Filling %s from %s failed", cfg.entity_id, source_entity_id)
        _notify(
            hass,
            f"Could not fill the statistics of {describe(hass, cfg.entity_id, cfg.name)} "
            f"from {source_entity_id}: {err}",
            f"{DOMAIN}_fill_{entry.entry_id}",
        )
        return
    if not filled.hours:
        return
    message = (
        f"Filled {filled.hours} hour(s) of statistics for "
        f"{describe(hass, cfg.entity_id, cfg.name)} from the history of "
        f"{source_entity_id}, which was renamed onto it."
    )
    _LOGGER.info("%s", message)
    _notify(hass, message, f"{DOMAIN}_fill_{entry.entry_id}")


@callback
def async_recompose(hass: HomeAssistant, entity_id: str) -> None:
    """Rewrite the titles composed from a renamed entity's display name.

    The title alone, never the data or options: that is what leaves the
    EntityConfig `_async_entry_updated` compares untouched, so a rename
    cannot take the compile lock for a full recompute.
    """
    entry = _entry_for(hass, entity_id)
    if entry is None:
        return
    cfg = hass.data[DOMAIN]["entry_configs"][entry.entry_id]
    for subentry in entry.subentries.values():
        if subentry.data.get(CONF_NAME) is not None:
            continue
        if subentry.subentry_type == SUBENTRY_SENSOR:
            title = sensor_title(hass, cfg, spec_from(subentry.data))
        elif subentry.subentry_type == SUBENTRY_STATE:
            title = state_title(hass, cfg)
        else:
            continue
        if title != subentry.title:
            hass.config_entries.async_update_subentry(entry, subentry, title=title)
    title = describe(hass, cfg.entity_id, cfg.name)
    if title != entry.title:
        hass.config_entries.async_update_entry(entry, title=title)


def missing_issue_id(entry: ConfigEntry) -> str:
    return f"missing_entity_{entry.entry_id}"


@callback
def async_review_missing(hass: HomeAssistant) -> None:
    """Raise the repair for an entry whose entity has neither registry entry nor state."""
    # Never while starting: the entity's own integration may not have loaded.
    if hass.state is not CoreState.running:
        return
    registry = er.async_get(hass)
    for entry_id, cfg in hass.data[DOMAIN]["entry_configs"].items():
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            continue
        gone = (
            registry.async_get(cfg.entity_id) is None
            and hass.states.get(cfg.entity_id) is None
        )
        if not gone:
            async_delete_issue(hass, DOMAIN, missing_issue_id(entry))
            continue
        async_create_issue(
            hass,
            DOMAIN,
            missing_issue_id(entry),
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="missing_entity",
            translation_placeholders={
                "entity": describe(hass, cfg.entity_id, cfg.name)
            },
        )


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Install the listeners. Called once, from the integration's setup."""

    @callback
    def registry_filter(event_data: er.EventEntityRegistryUpdatedData) -> bool:
        if event_data["action"] not in FOLLOWED_ACTIONS:
            return False
        # Either end: the old ID for a rename we follow, the new one for a
        # rename onto an entity we record.
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
            elif (entry := _entry_for(hass, new)) is not None:
                await async_fill(hass, entry, old, event.time_fired_timestamp)
            elif _owned(hass, new):
                _LOGGER.warning(
                    "%s was renamed onto %s, which is configured in "
                    "configuration.yaml; its history was not filled in, because "
                    "a YAML entity has no config entry to record the fill in",
                    old,
                    new,
                )
        elif (
            data["action"] == "update"
            and "device_id" in data["changes"]
            and (entry := _entry_for(hass, data["entity_id"])) is not None
        ):
            # The reload rebuilds the sensors, and it is construction that
            # reads the source's device and names them against it. Scheduled,
            # not awaited: core's advice for an integration reloading itself,
            # and a raise here would skip the review below.
            hass.config_entries.async_schedule_reload(entry.entry_id)
        # Not part of the chain above: a rename can move the entity ID and
        # the name in one event, and the titles follow the name either way.
        # Both names, because `display_name` resolves either of them.
        if data["action"] == "update" and not _NAMES.isdisjoint(data["changes"]):
            async_recompose(hass, data["entity_id"])
        async_review_missing(hass)

    @callback
    def state_removed_filter(event_data: EventStateChangedData) -> bool:
        return event_data["new_state"] is None and _owned(hass, event_data["entity_id"])

    @callback
    def state_removed(_event: Event[EventStateChangedData]) -> None:
        async_review_missing(hass)

    # A plain lambda here would be run in the executor, and the issue
    # registry is event-loop only.
    @callback
    def started(_hass: HomeAssistant) -> None:
        async_review_missing(hass)

    # `hass.bus.async_listen`, never `async_track_entity_registry_updated_event`:
    # that helper keys a rename on the *old* entity ID, so it never reaches a
    # rename onto an entity we record, which is the case async_fill exists for.
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, registry_updated, event_filter=registry_filter
    )
    hass.bus.async_listen(
        EVENT_STATE_CHANGED, state_removed, event_filter=state_removed_filter
    )
    async_at_started(hass, started)
