"""Long-term statistics for discrete entities."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.typing import ConfigType

from .compiler import Compiler
from .config import CONFIG_SCHEMA, EntityConfig  # noqa: F401  (CONFIG_SCHEMA is the HA hook)
from .const import BACKLOG_THRESHOLD, DOMAIN
from .registry import Registry

_LOGGER = logging.getLogger(__name__)

__all__ = ["CONFIG_SCHEMA", "async_setup"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up discrete_stats from YAML."""
    configs: list[EntityConfig] = config.get(DOMAIN, [])

    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    # Serialises compilation. The compiler reads a statistic's cumulative base
    # back from the recorder, so two overlapping compiles of the same entity
    # could both read the same stale base before either writes, and the second
    # would restart the series at zero. The compiler drains the recorder before
    # returning, which makes sequential compiles safe; this lock is what makes
    # them sequential when the hourly timer and the recompute service coincide.
    lock = asyncio.Lock()

    data: dict[str, Any] = {
        "configs": configs,
        "registry": registry,
        "compiler": compiler,
        "lock": lock,
    }
    hass.data[DOMAIN] = data

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
            for cfg in data["configs"]:
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

    return True
