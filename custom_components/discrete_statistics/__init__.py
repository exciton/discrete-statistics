"""Long-term statistics for discrete entities."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ENTITY_ID,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.typing import ConfigType

from .compiler import Compiler
from .config import CONFIG_SCHEMA, EntityConfig, entity_config_from_entry, is_configured
# CONFIG_SCHEMA is the HA hook: HA looks it up by name on this module to
# validate the YAML block, so it must stay imported even though nothing here
# calls it directly.
from .const import BACKLOG_THRESHOLD, DOMAIN

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "CONFIG_SCHEMA",
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
    "async_remove_entry",
]

SERVICE_RECOMPUTE = "recompute"

RECOMPUTE_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_id,
        vol.Optional("start"): cv.datetime,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up discrete_statistics from YAML."""
    configs: list[EntityConfig] = config.get(DOMAIN, [])

    compiler = Compiler(hass)

    # Serialises compilation. The compiler reads a statistic's cumulative base
    # back from the recorder, so two overlapping compiles of the same entity
    # could both read the same stale base before either writes, and the second
    # would restart the series at zero. The compiler drains the recorder before
    # returning, which makes sequential compiles safe; this lock is what makes
    # them sequential when the hourly timer and the recompute service coincide.
    lock = asyncio.Lock()

    data: dict[str, Any] = {
        "yaml_configs": configs,
        "entry_configs": {},
        "compiler": compiler,
        "lock": lock,
    }
    hass.data[DOMAIN] = data

    def _all_configs() -> list[EntityConfig]:
        """Every configured entity, YAML first, then config entries.

        An entity appears at most once: the flow refuses one that YAML
        owns, and async_setup_entry fails an entry that clashes with a
        YAML block added later. Two configurations for one entity resolve
        the same raw states through different disposition tables and write
        conflicting values to the same statistic IDs.
        """
        return [*data["yaml_configs"], *data["entry_configs"].values()]

    data["all_configs"] = _all_configs

    async def _async_compile_all(_now: Any = None) -> None:
        backlog = get_instance(hass).backlog
        if backlog > BACKLOG_THRESHOLD:
            _LOGGER.debug(
                "Skipping run; recorder backlog is %s (threshold %s)",
                backlog,
                BACKLOG_THRESHOLD,
            )
            return
        async with lock:
            for cfg in data["all_configs"]():
                try:
                    hours = await compiler.async_compile_incremental(cfg)
                except Exception:  # noqa: BLE001 - a bad entity must not stop the rest
                    _LOGGER.exception("Compiling %s failed", cfg.entity_id)
                else:
                    if hours:
                        _LOGGER.debug("Compiled %s hours for %s", hours, cfg.entity_id)

    data["compile_all"] = _async_compile_all

    # Hourly at :03 — between the recorder's own :00:10 and :05:10 tasks.
    # The offset is not load-bearing; the trailing recompute window is what
    # guarantees completeness.
    unsub_time_change = async_track_time_change(
        hass, _async_compile_all, minute=3, second=0
    )
    # async_track_time_change does not cancel itself on shutdown, so without
    # this the timer lingers past HA stop (and fails teardown in tests).
    hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, lambda _event: unsub_time_change()
    )

    async def _async_on_started(_event: Event) -> None:
        await _async_compile_all()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _async_on_started)

    async def _async_recompute(call: ServiceCall) -> None:
        entity_id = call.data.get("entity_id")
        if entity_id is None:
            targets = list(data["all_configs"]())
        else:
            targets = [c for c in data["all_configs"]() if c.entity_id == entity_id]
            if not targets:
                raise ServiceValidationError(
                    f"{entity_id} is not configured for discrete_statistics"
                )

        start_dt = call.data.get("start")
        start = start_dt.timestamp() if start_dt is not None else None

        # Same lock the hourly run uses. Two overlapping compiles of one
        # entity can both read the same stale cumulative base, and the second
        # restarts that statistic's series at zero. asyncio.Lock is NOT
        # reentrant, so this must never be acquired from inside a context that
        # already holds it — the service handler is always called from
        # outside, never from within compile_all.
        async with data["lock"]:
            for cfg in targets:
                # Logged at INFO, unlike the scheduled run's DEBUG: someone
                # invoked this by hand and should be able to confirm it ran
                # without first turning on debug logging.
                hours = await compiler.async_compile(cfg, start)
                _LOGGER.info(
                    "Recompute: compiled %s hour(s) for %s from %s",
                    hours,
                    cfg.entity_id,
                    "the earliest retained state"
                    if start_dt is None
                    else start_dt.isoformat(),
                )

    hass.services.async_register(
        DOMAIN, SERVICE_RECOMPUTE, _async_recompute, schema=RECOMPUTE_SCHEMA
    )

    return True


def _clash_issue_id(entry: ConfigEntry) -> str:
    return f"yaml_clash_{entry.entry_id}"


def _describe(cfg: EntityConfig) -> str:
    """Name an entity the way a notification's reader thinks of it.

    The name is what they typed and what labels their charts; the entity ID
    is what they search for in the log and in Settings > Statistics. A helper
    with no name of its own has nothing to distinguish, so the ID stands
    alone rather than being printed twice.
    """
    if cfg.name and cfg.name != cfg.entity_id:
        return f"{cfg.name} ({cfg.entity_id})"
    return cfg.entity_id


async def _async_compile_and_notify(
    hass: HomeAssistant, entry: ConfigEntry, cfg: EntityConfig, *, full: bool
) -> None:
    """Compile one entity and report the outcome as a notification.

    Runs as a background task, so an exception here would otherwise surface
    only as an unhandled-task traceback after a Submit that looked fine.
    One notification id per entry, so repeated edits replace rather than
    stack.
    """
    data = hass.data[DOMAIN]
    compiler: Compiler = data["compiler"]

    try:
        # The same lock the hourly run uses; a full recompute can hold it
        # for minutes and the hourly run simply awaits it. Never acquired
        # from inside compile_all - asyncio.Lock is not reentrant.
        async with data["lock"]:
            if full:
                hours = await compiler.async_compile(cfg, None)
            else:
                hours = await compiler.async_compile_incremental(cfg)
    except Exception as err:  # noqa: BLE001 - reported, not swallowed
        _LOGGER.exception("Compiling %s failed", cfg.entity_id)
        message = f"Could not compile statistics for {_describe(cfg)}: {err}"
    else:
        message = (
            f"Compiled {hours} hour(s) of statistics for {_describe(cfg)}."
            if hours
            else f"No history to compile yet for {_describe(cfg)}."
        )
        _LOGGER.info("%s", message)

    persistent_notification.async_create(
        hass,
        message,
        title="Discrete Statistics",
        notification_id=f"{DOMAIN}_{entry.entry_id}",
    )


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options and recompile only when attribution changed.

    add_update_listener fires on every async_update_entry, not only on an
    options change - including a title-only rename from the Helpers list.
    Comparing the freshly-built EntityConfig against the one on file is what
    keeps a cosmetic rename from taking the shared lock for a full recompute
    and raising a notification nobody asked for. Of what is left, only
    `default` changes past attribution; `payload` rebuilds statistic metadata
    on every compile, so a `name`-only change reaches the display name on the
    next ordinary run with no rewrite needed.

    Deliberately not hass.config_entries.async_reload() for the recompute
    path: reload re-runs async_setup_entry, whose compile is incremental, and
    a changed disposition reattributes every past hour. Updating in place and
    running the full recompute here is the difference between a corrected
    history and a seam at the moment of the edit.
    """
    data = hass.data[DOMAIN]
    old_cfg = data["entry_configs"].get(entry.entry_id)
    cfg = entity_config_from_entry(entry.data, entry.options)
    if cfg == old_cfg:
        return
    data["entry_configs"][entry.entry_id] = cfg

    # Keep the Helpers row's title matching the name option; async_setup_entry
    # only sets the title once, at creation. This call re-fires this very
    # listener, which is safe *only* because of the equality check above: on
    # that re-entry entity_config_from_entry reproduces the same cfg, so it
    # matches what was just stored and the listener returns immediately
    # instead of looping or recompiling a second time. Do not remove the
    # equality check while this call stays - it looks unrelated but it is
    # what stops the recursion.
    title = cfg.name or cfg.entity_id
    if title != entry.title:
        hass.config_entries.async_update_entry(entry, title=title)

    if old_cfg is not None and cfg.default == old_cfg.default:
        return

    entry.async_create_background_task(
        hass,
        _async_compile_and_notify(hass, entry, cfg, full=True),
        name=f"{DOMAIN} recompute {cfg.entity_id}",
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one UI-configured entity."""
    data = hass.data[DOMAIN]
    cfg = entity_config_from_entry(entry.data, entry.options)

    # YAML is read at startup and can name an entity a helper already owns.
    # YAML wins because async_setup runs first; failing setup here is what
    # keeps the entry out of all_configs(), so the exclusion cannot drift
    # from the failure.
    if is_configured(data["yaml_configs"], cfg.entity_id):
        async_create_issue(
            hass,
            DOMAIN,
            _clash_issue_id(entry),
            is_fixable=False,
            severity=IssueSeverity.ERROR,
            translation_key="yaml_clash",
            translation_placeholders={"entity_id": cfg.entity_id},
        )
        raise ConfigEntryError(
            f"{cfg.entity_id} is also configured in configuration.yaml. "
            "Remove one of the two configurations."
        )

    async_delete_issue(hass, DOMAIN, _clash_issue_id(entry))
    data["entry_configs"][entry.entry_id] = cfg
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    # Only when Home Assistant is already running, which means a genuine
    # creation or reload. At boot the EVENT_HOMEASSISTANT_STARTED handler
    # compiles every config, and doing it here too would compile twice.
    if hass.state is CoreState.running:
        entry.async_create_background_task(
            hass,
            _async_compile_and_notify(hass, entry, cfg, full=False),
            name=f"{DOMAIN} compile {cfg.entity_id}",
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop compiling one entity. The shared machinery stays up."""
    hass.data[DOMAIN]["entry_configs"].pop(entry.entry_id, None)
    async_delete_issue(hass, DOMAIN, _clash_issue_id(entry))
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Log what removal kept. Nothing here deletes statistics.

    Also clears any yaml_clash issue this entry left behind:
    async_unload_entry never runs for an entry stuck in SETUP_ERROR, so
    removal is the only remaining point that can retire the issue. A no-op
    when there is nothing to delete, so this is safe on the ordinary path.
    """
    async_delete_issue(hass, DOMAIN, _clash_issue_id(entry))
    _LOGGER.info(
        "Removed %s from %s. Its statistics are kept; delete them in "
        "Settings > System > Tools > Statistics if you no longer want them",
        entry.data[CONF_ENTITY_ID],
        DOMAIN,
    )
