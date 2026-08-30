"""Convert bucketed values into cumulative statistic payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.recorder.models import StatisticMeanType

from .config import EntityConfig
from .const import DOMAIN, HOUR, METRIC_COUNT, METRIC_SECONDS
from .statistic_ids import build as build_statistic_id

_METRIC_LABEL = {METRIC_SECONDS: "duration", METRIC_COUNT: "count"}


def metadata_for(
    cfg: EntityConfig, state: str, metric: str, statistic_id: str
) -> dict[str, Any]:
    """Return StatisticMetaData for one statistic."""
    display = cfg.name or cfg.entity_id
    return {
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": f"{display}: {state} ({_METRIC_LABEL[metric]})",
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": "s" if metric == METRIC_SECONDS else None,
        "unit_class": "duration" if metric == METRIC_SECONDS else None,
    }


def build_payloads(
    cfg: EntityConfig,
    buckets: dict[tuple[str, float], tuple[float, int]],
    window_start: float,
    window_end: float,
    base_sums: dict[str, float],
) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]], str, str]]:
    """Return {statistic_id: (metadata, rows, state, metric)} with cumulative sums.

    Rows are dense: every hour in [window_start, window_end) gets a row for
    every state seen in the window, carrying the running sum forward even
    when the hourly delta is zero. Sparse rows would make the recorder's
    `change` computation attribute a delta to the wrong bucket.
    """
    hours: list[float] = []
    hour = window_start
    while hour < window_end:
        hours.append(hour)
        hour += HOUR

    states = sorted({state for state, _ in buckets})

    payloads: dict[str, tuple[dict[str, Any], list[dict[str, Any]], str, str]] = {}

    for state in states:
        for metric, index in ((METRIC_SECONDS, 0), (METRIC_COUNT, 1)):
            statistic_id = build_statistic_id(cfg.entity_id, state, metric)
            running = base_sums.get(statistic_id, 0.0)
            rows: list[dict[str, Any]] = []
            for hour in hours:
                running += buckets.get((state, hour), (0.0, 0))[index]
                rows.append(
                    {
                        "start": datetime.fromtimestamp(hour, tz=timezone.utc),
                        "sum": running,
                    }
                )
            payloads[statistic_id] = (
                metadata_for(cfg, state, metric, statistic_id),
                rows,
                state,
                metric,
            )

    return payloads
