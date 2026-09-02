"""Convert bucketed values into cumulative statistic payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.recorder.models import StatisticMeanType

from .config import EntityConfig
from .const import DOMAIN, HOUR, METRIC_COUNT, METRIC_DURATION, NO_DATA
from .statistic_ids import build as build_statistic_id

_METRIC_LABEL = {METRIC_DURATION: "duration", METRIC_COUNT: "count"}


def metadata_for(
    cfg: EntityConfig, state: str, metric: str, statistic_id: str
) -> dict[str, Any]:
    """Return StatisticMetaData for one statistic."""
    display = cfg.name or cfg.entity_id
    return {
        # Both a sum and a mean. The sum is the cumulative total charts read
        # through `stat_types: change`; the mean, min and max are the hourly
        # value itself, which is what lets the recorder's day/week/month
        # rollup answer "average hours on per hour" and "busiest hour".
        "has_mean": True,
        "mean_type": StatisticMeanType.ARITHMETIC,
        "has_sum": True,
        "name": f"{display}: {state} ({_METRIC_LABEL[metric]})",
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": "h" if metric == METRIC_DURATION else None,
        "unit_class": "duration" if metric == METRIC_DURATION else None,
    }


def build_payloads(
    cfg: EntityConfig,
    buckets: dict[tuple[str, float], tuple[float, int]],
    window_start: float,
    window_end: float,
    base_sums: dict[str, float],
    known_states: frozenset[str] = frozenset(),
) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]], str, str]]:
    """Return {statistic_id: (metadata, rows, state, metric)} with cumulative sums.

    Rows are dense over every KNOWN statistic, not merely every state seen
    in this window: each hour in [window_start, window_end) gets a row for
    every state in `known_states` as well as every state in `buckets`,
    carrying the running sum forward even when the hourly delta is zero.

    Density matters twice over. Sparse rows would make the recorder's
    `change` computation attribute a delta to the wrong bucket. And a
    statistic left out of an hour would lose its cumulative base on the
    next window: the caller reads the base from the hour immediately
    before the window, finds nothing, restarts from zero, and the
    monotonic sum goes backwards.

    NO_DATA is the one state with no `count` metric. Nothing ever emits a
    transition INTO it - it exists only as the fallback when no state can be
    carried in - so the count would be a permanent zero written densely
    forever.
    """
    hours: list[float] = []
    hour = window_start
    while hour < window_end:
        hours.append(hour)
        hour += HOUR

    states = sorted({state for state, _ in buckets} | set(known_states))

    payloads: dict[str, tuple[dict[str, Any], list[dict[str, Any]], str, str]] = {}

    for state in states:
        # The bucketer works in seconds because that is what timestamp
        # arithmetic yields; durations are converted once, here, so an hourly
        # bucket of solid state reads as 1.0 rather than 3600.
        for metric, index, scale in (
            (METRIC_DURATION, 0, 1.0 / HOUR),
            (METRIC_COUNT, 1, 1.0),
        ):
            if state == NO_DATA and metric == METRIC_COUNT:
                continue
            statistic_id = build_statistic_id(cfg.entity_id, state, metric)
            running = base_sums.get(statistic_id, 0.0)
            rows: list[dict[str, Any]] = []
            for hour in hours:
                value = buckets.get((state, hour), (0.0, 0))[index] * scale
                running += value
                rows.append(
                    {
                        "start": datetime.fromtimestamp(hour, tz=timezone.utc),
                        "sum": running,
                        # An hour holds exactly one value, so its mean, min
                        # and max are all that value. They exist so the
                        # recorder can reduce the hours to a day, week or
                        # month: mean of the hourly means is the average
                        # hourly on-time, min and max are the quietest and
                        # busiest hours. `_reduce_statistics` reads the min
                        # and max columns rather than deriving them, so
                        # storing all three is not redundancy.
                        #
                        # The density invariant is what makes the mean
                        # correct: an hour in which nothing happened still
                        # gets a row of 0.0, so the average is over every
                        # hour in the period rather than only the active ones.
                        "mean": value,
                        "min": value,
                        "max": value,
                    }
                )
            payloads[statistic_id] = (
                metadata_for(cfg, state, metric, statistic_id),
                rows,
                state,
                metric,
            )

    return payloads
