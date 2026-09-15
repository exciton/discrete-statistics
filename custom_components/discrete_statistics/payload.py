"""Convert bucketed values into cumulative statistic payloads, one row where something changed."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, NamedTuple

from homeassistant.components.recorder.models import StatisticMeanType

from .config import EntityConfig
from .const import DOMAIN, HOUR, METRIC_COUNT, METRIC_DURATION
from .statistic_ids import build as build_statistic_id
from .statistic_ids import parse, state_token

# Short because the name sits in a chart legend, and because the duration
# label matches the unit the statistic already carries.
_METRIC_LABEL = {METRIC_DURATION: "h", METRIC_COUNT: "#"}


class Payload(NamedTuple):
    """One statistic's metadata, the rows to write, and the sum it ends on.

    The ending sum is not the last row's: a row is written only where
    something changed, so a window that writes nothing at all still hands
    the next chunk the sum it reached.
    """

    metadata: dict[str, Any]
    rows: list[dict[str, Any]]
    ending_sum: float


class _Planned(NamedTuple):
    """One statistic this window must write, and how to label it."""

    token: str
    metric: str
    name: str


def compose_name(display: str, state: str, metric: str) -> str:
    """Build a statistic's display name from its parts.

    Colons are stripped from the state, and only from the state. `rename`
    finds the display half by splitting on the LAST `": "`, which is right
    however many colons a display name has - but only while the state has
    none. A state containing one would move that boundary and truncate
    itself on the next rename, silently, and only for statistics the window
    did not see. Removing the character outright is provably enough;
    collapsing `": "` to `":"` is not, because `"a:  b"` survives it.
    """
    return f"{display}: {state.replace(':', '')} ({_METRIC_LABEL[metric]})"


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


def readable_state(stored_name: str, token: str) -> str:
    """Recover a state from the name its statistic already carries.

    An ID holds only the token, so a state carried out of one would read
    `heatcool` where the entity says `heat_cool` - and that name would then
    be written for as long as nothing transitioned. The stored name still
    has the readable form, in the half `rename` leaves alone.

    Verified rather than trusted: the recovered text must tokenise back to
    the same token, or the name did not have the shape assumed and the token
    stands.
    """
    head, separator, _ = stored_name.rpartition(" (")
    if not separator:
        return token
    _, separator, state = head.rpartition(": ")
    if separator and state_token(state) == token:
        return state
    return token


def metadata_for(metric: str, statistic_id: str, name: str) -> dict[str, Any]:
    """Return StatisticMetaData for one statistic."""
    return {
        # The sum is what `stat_types: change` reads. No mean: the recorder's
        # reduction skips absent rows, so over sparse rows a mean would be
        # over the hours the state occurred, never over the period.
        "has_mean": False,
        "mean_type": StatisticMeanType.NONE,
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
    display: str | None = None,
    translate: Callable[[str], str] | None = None,
    standing: Mapping[str, Mapping[float, float]] | None = None,
) -> dict[str, Payload]:
    """Return {statistic_id: Payload} - metadata, rows, and the sum reached.

    Where no row stands, one is written for a non-zero value. Where one
    stands, it is rewritten only when the sum it holds differs from the
    sum computed here - a recompile that finds a state absent from an
    hour it was written into must rewrite that row with the carried sum,
    or its old sum stands ahead of every later one; a row the recorder
    would rewrite with itself is two statements for nothing. Float
    equality is the test because the same arithmetic over the same
    inputs yields the same float, and a sum that differs at all is a sum
    the chart would read differently.

    The running sum advances whether or not a row is written, so a
    statistic with nothing to write returns no rows and its sum is
    unchanged.
    """
    existing = existing or {}
    # The caller resolves this: it is the entity's own name where there is
    # one, which needs a `hass` this module deliberately does not have.
    display = display or cfg.name or cfg.entity_id
    # Renders a state the way Home Assistant would. Resolved by the caller,
    # which has the `hass` and the entity's device class; identity here so
    # this module stays testable on its own.
    translate = translate or (lambda state: state)
    hours: list[float] = []
    hour = window_start
    while hour < window_end:
        hours.append(hour)
        hour += HOUR

    folded, labels = _fold(buckets)

    planned: dict[str, _Planned] = {}

    for token, state in labels.items():
        for metric in (METRIC_DURATION, METRIC_COUNT):
            statistic_id = build_statistic_id(cfg.entity_id, state, metric)
            planned[statistic_id] = _Planned(
                token, metric, compose_name(display, translate(state), metric)
            )

    for statistic_id, stored_name in existing.items():
        if statistic_id in planned:
            continue
        if (parts := parse(statistic_id)) is None:
            continue
        _, token, metric = parts
        # Not seen this window: the readable state survives only in the
        # stored name, so swap its display half rather than rebuilding it.
        planned[statistic_id] = _Planned(token, metric, rename(stored_name, display))

    payloads: dict[str, Payload] = {}
    for statistic_id, plan in sorted(planned.items()):
        index, scale = (0, 1.0 / HOUR) if plan.metric == METRIC_DURATION else (1, 1.0)
        stands = standing.get(statistic_id, {}) if standing else {}
        running = base_sums.get(statistic_id, 0.0)
        rows: list[dict[str, Any]] = []
        for hour in hours:
            # Seconds to hours, converted once: a solid hour reads as 1.0.
            value = folded.get((plan.token, hour), (0.0, 0))[index] * scale
            running += value
            stood = stands.get(hour)
            # Where a row stands the value is beside the point: the sum
            # is what it holds, and rewriting it with the same one is
            # work the recorder does twice for no change.
            write = bool(value) if stood is None else running != stood
            if write:
                rows.append(
                    {
                        "start": datetime.fromtimestamp(hour, tz=timezone.utc),
                        "sum": running,
                    }
                )
        payloads[statistic_id] = Payload(
            metadata_for(plan.metric, statistic_id, plan.name), rows, running
        )

    return payloads
