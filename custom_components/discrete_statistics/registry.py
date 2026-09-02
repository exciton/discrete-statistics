"""Reverse mapping from statistic IDs to their parts.

Statistic IDs are built from slugified parts and cannot be parsed back
unambiguously, so the mapping is stored explicitly.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.registry"


class Registry:
    """Persistent map of statistic_id -> (entity_id, state, metric)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._statistics: dict[str, dict[str, str]] = {}

    async def async_load(self) -> None:
        """Load the registry from storage."""
        data = await self._store.async_load()
        self._statistics = (data or {}).get("statistics", {})

    async def _async_save(self) -> None:
        await self._store.async_save({"statistics": self._statistics})

    def is_empty(self) -> bool:
        """True when nothing is registered for any entity."""
        return not self._statistics

    def statistic_ids_for(self, entity_id: str) -> list[str]:
        """Return the known statistic IDs for an entity, sorted."""
        return sorted(
            statistic_id
            for statistic_id, parts in self._statistics.items()
            if parts["entity_id"] == entity_id
        )

    def describe(self, statistic_id: str) -> tuple[str, str, str] | None:
        """Return (entity_id, state, metric) for a statistic ID."""
        if (parts := self._statistics.get(statistic_id)) is None:
            return None
        return parts["entity_id"], parts["state"], parts["metric"]

    async def async_forget(self, statistic_ids: set[str]) -> None:
        """Drop statistic IDs from the registry. Idempotent.

        Only for statistics that no longer exist in the recorder. Forgetting
        one that does exist would drop it from `known_states` and end its
        density, which the next compile reads as a missing cumulative base.
        """
        if not (present := statistic_ids & self._statistics.keys()):
            return
        for statistic_id in present:
            del self._statistics[statistic_id]
        await self._async_save()

    async def async_register(
        self, entity_id: str, entries: dict[str, tuple[str, str]]
    ) -> None:
        """Record statistic IDs for an entity. Idempotent."""
        changed = False
        for statistic_id, (state, metric) in entries.items():
            record = {"entity_id": entity_id, "state": state, "metric": metric}
            if self._statistics.get(statistic_id) != record:
                self._statistics[statistic_id] = record
                changed = True
        if changed:
            await self._async_save()
