"""Compile recorder history into external statistics."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .bucketer import bucket, hour_start
from .canonicalise import canonicalise
from .config import EntityConfig
from .const import HOUR
from .payload import build_payloads
from .registry import Registry

_LOGGER = logging.getLogger(__name__)

# Recompute this many trailing hours on every run, so a state committed by
# the recorder after we first read its hour is still picked up.
TRAILING_HOURS = 3

# Compile in windows of this size to bound memory during a long backfill.
CHUNK_HOURS = 24 * 7

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# `state_changes_during_period` selects the start-time state with a strict
# `last_updated_ts < start_time` and the changes with a strict
# `last_updated_ts > start_time`, so a state recorded exactly on the window
# boundary is returned by neither. Asking from a moment earlier makes it fall
# into the changes half; canonicalise() folds everything at or before
# window_start into the carried state, so the extra rows are harmless.
#
# The margin is a half second, not a whole one. window_start is always an
# exact multiple of 3600.0, so subtracting 1.0 would land on another exact
# integer second - a timestamp a state is just as likely to carry, which
# would move the hole rather than close it. Anything finer than a
# microsecond is no use either: the value goes through
# datetime.fromtimestamp, whose resolution is microseconds, so a 1-ULP
# offset at epoch scale (~2.4e-7 s) rounds straight back to window_start.
START_MARGIN = 0.5


def _as_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


class Compiler:
    """Compile one entity's history into statistics."""

    def __init__(self, hass: HomeAssistant, registry: Registry) -> None:
        self._hass = hass
        self._registry = registry

    async def async_compile_incremental(self, cfg: EntityConfig) -> int:
        """Compile from the watermark, recomputing the trailing window."""
        watermark = await self._async_watermark(cfg.entity_id)
        if watermark is None:
            start = await self._async_earliest_state_ts(cfg.entity_id)
            if start is None:
                return 0
        else:
            start = watermark - (TRAILING_HOURS - 1) * HOUR
        return await self.async_compile(cfg, start)

    async def async_compile(
        self, cfg: EntityConfig, start: float | None, end: float | None = None
    ) -> int:
        """Compile [start, end) for one entity. Returns hours compiled."""
        if start is None:
            start = await self._async_earliest_state_ts(cfg.entity_id)
            if start is None:
                return 0

        window_start = hour_start(start)
        # Only completed hours are emitted.
        window_end = hour_start(end if end is not None else dt_util.utcnow().timestamp())
        if window_end <= window_start:
            return 0

        base_sums = await self._async_base_sums(cfg.entity_id, window_start)

        compiled = 0
        chunk_start = window_start
        while chunk_start < window_end:
            chunk_end = min(chunk_start + CHUNK_HOURS * HOUR, window_end)
            base_sums = await self._async_compile_chunk(
                cfg, chunk_start, chunk_end, base_sums
            )
            compiled += int((chunk_end - chunk_start) / HOUR)
            chunk_start = chunk_end

        # async_add_external_statistics only enqueues. Drain before returning
        # so a subsequent compile of this entity reads a base that includes
        # everything we just wrote - otherwise it would miss them and restart
        # the cumulative series at zero. On the normal path only: if the loop
        # above raised, nothing was written and there is nothing to drain.
        await get_instance(self._hass).async_block_till_done()

        return compiled

    async def _async_compile_chunk(
        self,
        cfg: EntityConfig,
        window_start: float,
        window_end: float,
        base_sums: dict[str, float],
    ) -> dict[str, float]:
        """Compile one chunk, returning the sums to carry into the next."""
        history = await get_instance(self._hass).async_add_executor_job(
            state_changes_during_period,
            self._hass,
            _as_datetime(window_start - START_MARGIN),
            _as_datetime(window_end),
            cfg.entity_id,
            True,  # no_attributes
            False,  # descending
            None,  # limit
            True,  # include_start_time_state
        )
        rows = history.get(cfg.entity_id, [])

        carried, transitions = canonicalise(cfg, rows, window_start)
        if carried is None:
            _LOGGER.warning(
                "No recoverable state for %s at %s; attributing the span to no_data",
                cfg.entity_id,
                _as_datetime(window_start).isoformat(),
            )

        buckets = bucket(carried, transitions, window_start, window_end)

        # Every statistic already registered for this entity must get a row
        # in every hour, even when this window saw nothing of its state.
        # Otherwise the next window finds no row in the hour before it,
        # restarts that statistic's cumulative sum from zero and loses the
        # running total.
        known_states = {
            parts[1]
            for statistic_id in self._registry.statistic_ids_for(cfg.entity_id)
            if (parts := self._registry.describe(statistic_id)) is not None
        }
        payloads = build_payloads(
            cfg, buckets, window_start, window_end, base_sums, frozenset(known_states)
        )

        await self._registry.async_register(
            cfg.entity_id,
            {
                statistic_id: (state, metric)
                for statistic_id, (_, _, state, metric) in payloads.items()
            },
        )

        next_sums = dict(base_sums)
        for statistic_id, (metadata, statistic_rows, _, _) in payloads.items():
            async_add_external_statistics(self._hass, metadata, statistic_rows)
            next_sums[statistic_id] = statistic_rows[-1]["sum"]

        return next_sums

    async def _async_watermark(self, entity_id: str) -> float | None:
        """Return the newest compiled hour for an entity, or None.

        Takes the max across every registered statistic: density is
        guaranteed only for statistics known at the time a window was
        compiled, so any single ID can lag the others.
        """
        statistic_ids = self._registry.statistic_ids_for(entity_id)
        if not statistic_ids:
            return None
        newest: float | None = None
        for statistic_id in statistic_ids:
            result = await get_instance(self._hass).async_add_executor_job(
                get_last_statistics,
                self._hass,
                1,
                statistic_id,
                True,
                {"sum"},
            )
            if rows := result.get(statistic_id):
                start = rows[0]["start"]
                if newest is None or start > newest:
                    newest = start
        return newest

    async def _async_base_sums(
        self, entity_id: str, window_start: float
    ) -> dict[str, float]:
        """Return cumulative sums for the hour immediately before the window."""
        statistic_ids = self._registry.statistic_ids_for(entity_id)
        if not statistic_ids:
            return {}
        result = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            _as_datetime(window_start - HOUR),
            _as_datetime(window_start),
            set(statistic_ids),
            "hour",
            None,
            {"sum"},
        )
        return {
            statistic_id: rows[-1]["sum"]
            for statistic_id, rows in result.items()
            if rows and rows[-1].get("sum") is not None
        }

    async def _async_earliest_state_ts(self, entity_id: str) -> float | None:
        """Return the timestamp of the oldest retained state, or None."""
        history = await get_instance(self._hass).async_add_executor_job(
            state_changes_during_period,
            self._hass,
            EPOCH,
            None,
            entity_id,
            True,
            False,
            1,
            False,
        )
        if not (rows := history.get(entity_id)):
            return None
        return rows[0].last_changed_timestamp
