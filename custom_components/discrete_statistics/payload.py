"""Convert bucketed values into cumulative statistic payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from homeassistant.components.recorder.models import StatisticMeanType

from .config import EntityConfig
from .const import DOMAIN, HOUR, METRIC_COUNT, METRIC_DURATION, NO_DATA
from .statistic_ids import build as build_statistic_id
from .statistic_ids import parse, state_token

_METRIC_LABEL = {METRIC_DURATION: "duration", METRIC_COUNT: "count"}
_NO_DATA_TOKEN = state_token(NO_DATA)

Payload = tuple[dict[str, Any], list[dict[str, Any]]]


def display_name(cfg: EntityConfig) -> str:
    """What labels this entity's charts."""
    return cfg.name or cfg.entity_id


def compose_name(cfg: EntityConfig, state: str, metric: str) -> str:
    """Build a statistic's display name from its parts."""
    return f"{display_name(cfg)}: {state} ({_METRIC_LABEL[metric]})"


def rename(stored: str, display: str) -> str:
    """Swap the display half of an existing statistic name.

    The state half cannot be rebuilt from the ID - the ID holds only the
    single-token form - so a rename replaces everything before the last
    `": "` and leaves the rest untouched. The last, not the first: a display
    name containing a colon would otherwise leave a fragment behind. A name
    that does not carry the separator is left exactly as it is rather than
    mangled.
    """
    head, sep, tail = stored.rpartition(": ")
    if not sep or not head:
        return stored
    return f"{display}: {tail}"


def metadata_for(metric: str, statistic_id: str, name: str) -> dict[str, Any]:
    """Return StatisticMetaData for one statistic."""
    return {
        # Both a sum and a mean. The sum is the cumulative total charts read
        # through `stat_types: change`; the mean, min and max are the hourly
        # value itself, which is what lets the recorder's day/week/month
        # rollup answer "average hours on per hour" and "busiest hour".
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

    Two canonical states with the same token are one statistic - the token
    is what the ID carries - so their seconds and counts must be added.
    Keying payloads by state instead would let the second state overwrite
    the first, and the hour would no longer total wall-clock time.

    Also returns a representative raw state per token, for the display name.
    Sorted so the choice is stable rather than dependent on bucket order.
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
    the states seen in this window. `existing` maps each of the entity's
    statistic IDs to the display name the recorder currently holds for it,
    and every one of them gets a row in each hour - carrying the running sum
    forward, and the hourly value at zero, even when nothing happened.

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
    existing = existing or {}
    hours: list[float] = []
    hour = window_start
    while hour < window_end:
        hours.append(hour)
        hour += HOUR

    folded, labels = _fold(buckets)
    display = display_name(cfg)

    # (statistic_id, token, metric, name) for everything this window must write.
    planned: dict[str, tuple[str, str, str]] = {}

    for token, state in labels.items():
        for metric in (METRIC_DURATION, METRIC_COUNT):
            if token == _NO_DATA_TOKEN and metric == METRIC_COUNT:
                continue
            statistic_id = build_statistic_id(cfg.entity_id, state, metric)
            planned[statistic_id] = (token, metric, compose_name(cfg, state, metric))

    for statistic_id, stored_name in existing.items():
        if statistic_id in planned:
            continue
        if (parts := parse(statistic_id)) is None:
            continue
        _, token, metric = parts
        if token == _NO_DATA_TOKEN and metric == METRIC_COUNT:
            continue
        # Not seen this window, so the readable state is only available from
        # the name the recorder already holds. Swap the display half of it.
        planned[statistic_id] = (token, metric, rename(stored_name, display))

    payloads: dict[str, Payload] = {}
    for statistic_id, (token, metric, name) in sorted(planned.items()):
        index, scale = (0, 1.0 / HOUR) if metric == METRIC_DURATION else (1, 1.0)
        running = base_sums.get(statistic_id, 0.0)
        rows: list[dict[str, Any]] = []
        for hour in hours:
            # The bucketer works in seconds because that is what timestamp
            # arithmetic yields; durations are converted once, here, so an
            # hourly bucket of solid state reads as 1.0 rather than 3600.
            value = folded.get((token, hour), (0.0, 0))[index] * scale
            running += value
            rows.append(
                {
                    "start": datetime.fromtimestamp(hour, tz=timezone.utc),
                    "sum": running,
                    # An hour holds exactly one value, so its mean, min and
                    # max are all that value. They exist so the recorder can
                    # reduce the hours to a day, week or month: mean of the
                    # hourly means is the average hourly on-time, min and max
                    # are the quietest and busiest hours. `_reduce_statistics`
                    # reads the min and max columns rather than deriving them,
                    # so storing all three is not redundancy.
                    #
                    # The density invariant is what makes the mean correct: an
                    # hour in which nothing happened still gets a row of 0.0,
                    # so the average is over every hour in the period rather
                    # than only the active ones.
                    "mean": value,
                    "min": value,
                    "max": value,
                }
            )
        payloads[statistic_id] = (
            metadata_for(metric, statistic_id, name),
            rows,
        )

    return payloads
