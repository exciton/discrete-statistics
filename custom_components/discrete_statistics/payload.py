"""Convert bucketed values into cumulative statistic payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, NamedTuple

from homeassistant.components.recorder.models import StatisticMeanType

from .config import EntityConfig
from .const import DOMAIN, HOUR, METRIC_COUNT, METRIC_DURATION, NO_DATA
from .statistic_ids import build as build_statistic_id
from .statistic_ids import parse, state_token

_METRIC_LABEL = {METRIC_DURATION: "duration", METRIC_COUNT: "count"}
_NO_DATA_TOKEN = state_token(NO_DATA)

Payload = tuple[dict[str, Any], list[dict[str, Any]]]


class _Planned(NamedTuple):
    """One statistic this window must write, and how to label it."""

    token: str
    metric: str
    name: str


def display_name(cfg: EntityConfig) -> str:
    """What labels this entity's charts."""
    return cfg.name or cfg.entity_id


def compose_name(cfg: EntityConfig, state: str, metric: str) -> str:
    """Build a statistic's display name from its parts."""
    return f"{display_name(cfg)}: {state} ({_METRIC_LABEL[metric]})"


def rename(stored: str, display: str) -> str:
    """Swap the display half of an existing statistic name.

    The state half cannot be rebuilt from the ID, which holds only the
    token. Split on the LAST separator: a display name containing a colon
    would otherwise leave a fragment behind.
    """
    head, sep, tail = stored.rpartition(": ")
    if not sep or not head:
        return stored
    return f"{display}: {tail}"


def metadata_for(metric: str, statistic_id: str, name: str) -> dict[str, Any]:
    """Return StatisticMetaData for one statistic."""
    return {
        # Both: the sum is what `stat_types: change` reads, the mean/min/max
        # are what the day/week/month rollup averages.
        "has_mean": True,
        "mean_type": StatisticMeanType.ARITHMETIC,
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": "h" if metric == METRIC_DURATION else None,
        "unit_class": "duration" if metric == METRIC_DURATION else None,
    }


def _fold(
    buckets: dict[tuple[str, float], tuple[float, int]],
) -> tuple[dict[tuple[str, float], tuple[float, int]], dict[str, str]]:
    """Group buckets by state token, summing states that share one.

    Two states with the same token are one statistic, so their seconds and
    counts must ADD: keying by raw state would let the second overwrite the
    first and the hour would stop totalling wall-clock time. Also returns a
    representative raw state per token for the display name, sorted so the
    choice does not depend on bucket order.
    """
    folded: dict[tuple[str, float], tuple[float, int]] = {}
    labels: dict[str, str] = {}
    for (state, hour), (seconds, count) in sorted(buckets.items()):
        token = state_token(state)
        labels.setdefault(token, state)
        prev_seconds, prev_count = folded.get((token, hour), (0.0, 0))
        folded[(token, hour)] = (prev_seconds + seconds, prev_count + count)
    return folded, labels


def build_payloads(
    cfg: EntityConfig,
    buckets: dict[tuple[str, float], tuple[float, int]],
    window_start: float,
    window_end: float,
    base_sums: dict[str, float],
    existing: Mapping[str, str] | None = None,
) -> dict[str, Payload]:
    """Return {statistic_id: (metadata, rows)} with cumulative sums.

    Rows are dense over every statistic this entity ALREADY HAS, not merely
    the states seen in this window: `existing` maps its statistic IDs to the
    names the recorder holds, and each gets a row in every hour, carrying its
    sum forward at a zero hourly value. A statistic left out of an hour loses
    its cumulative base on the next window - the caller reads that base from
    the preceding hour, finds nothing, and restarts from zero.

    NO_DATA is the one state with no `count`: nothing ever transitions into
    it, so the count would be a permanent zero written densely forever.
    """
    existing = existing or {}
    hours: list[float] = []
    hour = window_start
    while hour < window_end:
        hours.append(hour)
        hour += HOUR

    folded, labels = _fold(buckets)
    display = display_name(cfg)

    planned: dict[str, _Planned] = {}

    for token, state in labels.items():
        for metric in (METRIC_DURATION, METRIC_COUNT):
            if token == _NO_DATA_TOKEN and metric == METRIC_COUNT:
                continue
            statistic_id = build_statistic_id(cfg.entity_id, state, metric)
            planned[statistic_id] = _Planned(
                token, metric, compose_name(cfg, state, metric)
            )

    for statistic_id, stored_name in existing.items():
        if statistic_id in planned:
            continue
        if (parts := parse(statistic_id)) is None:
            continue
        _, token, metric = parts
        if token == _NO_DATA_TOKEN and metric == METRIC_COUNT:
            continue
        # Not seen this window: the readable state survives only in the
        # stored name, so swap its display half rather than rebuilding it.
        planned[statistic_id] = _Planned(
            token, metric, rename(stored_name, display)
        )

    payloads: dict[str, Payload] = {}
    for statistic_id, plan in sorted(planned.items()):
        index, scale = (
            (0, 1.0 / HOUR) if plan.metric == METRIC_DURATION else (1, 1.0)
        )
        running = base_sums.get(statistic_id, 0.0)
        rows: list[dict[str, Any]] = []
        for hour in hours:
            # Seconds to hours, converted once: a solid hour reads as 1.0.
            value = folded.get((plan.token, hour), (0.0, 0))[index] * scale
            running += value
            rows.append(
                {
                    "start": datetime.fromtimestamp(hour, tz=timezone.utc),
                    "sum": running,
                    # An hour holds one value, so mean, min and max are all
                    # of them it. They only become interesting after the
                    # recorder reduces hours to a day: the average hourly
                    # on-time, and the quietest and busiest hours.
                    # `_reduce_statistics` reads the min and max columns
                    # rather than deriving them, so all three are needed.
                    "mean": value,
                    "min": value,
                    "max": value,
                }
            )
        payloads[statistic_id] = (
            metadata_for(plan.metric, statistic_id, plan.name),
            rows,
        )

    return payloads
