"""What to send with an "it isn't compiling" report.

One download from the entry's menu: the entry and the disposition table
the compiler builds from it, the statistics the recorder holds and where
they end, what the recorder holds of the entity's own history, and each
period sensor's state. Every recorder read is fenced on its own, since a
recorder in trouble is the moment someone downloads this.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .compiler import Compiler
from .config import entity_config_from_entry, is_configured
from .const import BACKLOG_THRESHOLD, DOMAIN, HOUR, SUBENTRY_SENSOR


def _iso(timestamp: float | None) -> str | None:
    return (
        None if timestamp is None else dt_util.utc_from_timestamp(timestamp).isoformat()
    )


async def _fenced(read: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    try:
        return await read()
    except Exception as err:  # noqa: BLE001 - the report is the point
        return {"error": f"{type(err).__name__}: {err}"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one entry."""
    data = hass.data[DOMAIN]
    compiler: Compiler = data["compiler"]
    cfg = entity_config_from_entry(entry.data, entry.options)
    entity_id = cfg.entity_id

    async def statistics() -> dict[str, Any]:
        existing, watermark = await compiler.async_compiled(entity_id)
        return {
            "ids": existing,
            "watermark": _iso(watermark),
            "watermark_end": _iso(None if watermark is None else watermark + HOUR),
        }

    async def recorder() -> dict[str, Any]:
        instance = get_instance(hass)
        state = hass.states.get(entity_id)
        return {
            "earliest_retained_state": _iso(
                await compiler.async_earliest_state_ts(entity_id)
            ),
            "current_state": None if state is None else state.state,
            "current_state_since": None
            if state is None
            else state.last_changed.isoformat(),
            "backlog": instance.backlog,
            "backlog_threshold": BACKLOG_THRESHOLD,
            "purge_keep_days": instance.keep_days,
            "time_zone": hass.config.time_zone,
        }

    registry = er.async_get(hass)
    sensors = []
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_SENSOR:
            continue
        registered = registry.async_get_entity_id("sensor", DOMAIN, subentry_id)
        state = None if registered is None else hass.states.get(registered)
        sensors.append(
            {
                "subentry_id": subentry_id,
                "title": subentry.title,
                "data": dict(subentry.data),
                "entity_id": registered,
                "state": None if state is None else state.state,
                "attributes": None if state is None else dict(state.attributes),
            }
        )

    integration = await async_get_integration(hass, DOMAIN)
    return {
        "version": integration.version,
        "entry": entry.as_dict(),
        "config": asdict(cfg),
        "configured_by": "yaml"
        if is_configured(data["yaml_configs"], entity_id)
        else "entry",
        "statistics": await _fenced(statistics),
        "recorder": await _fenced(recorder),
        "sensors": sensors,
        "yaml_entities": [c.entity_id for c in data["yaml_configs"]],
    }
