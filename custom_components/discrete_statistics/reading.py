"""One period sensor's value, from the sums at two edges and a live tail.

Pure: the coordinator fetches the sums and the tail, this module does the
arithmetic. A period is read as the card reads a bucket - the sum at its
start against the sum at its end - and the hours after the watermark, not
yet compiled, come from the compiler's own read path as a `Timeline`
tallied per state. The two never overlap: the statistics answer up to
the watermark end, the tail from it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import tzinfo
from typing import TYPE_CHECKING, Any, NamedTuple

from homeassistant.util import slugify

from .bucketer import tally
from .config import CONF_STATES, EntityConfig
from .const import (
    CONF_LIVE,
    CONF_METRIC,
    CONF_PERIOD,
    HOUR,
    METRIC_COUNT,
    METRIC_DURATION,
)
from .periods import bounds
from .statistic_ids import parse, state_token

if TYPE_CHECKING:
    from .compiler import Timeline

# Why a sensor is unavailable rather than zero: every state it asks for
# resolves to nothing under the entry's settings and none was ever
# recorded, so there is no series to read and the tail would never
# contribute.
REASON_NOT_RECORDED = "not recorded by this entry's settings"


class Spec(NamedTuple):
    """What a sensor subentry asks for."""

    states: tuple[str, ...]
    metric: str
    period: str
    live: bool


class Frame(NamedTuple):
    """What the recorder holds for the entity, fetched once per refresh."""

    existing: dict[str, str]
    watermark_end: float | None
    series_start: float | None


class Reading(NamedTuple):
    value: float | int | None
    period_start: float | None
    period_end: float | None
    reason: str | None


def spec_from(data: Mapping[str, Any]) -> Spec:
    return Spec(
        tuple(data.get(CONF_STATES) or ()),
        data.get(CONF_METRIC, METRIC_DURATION),
        data.get(CONF_PERIOD, "this_month"),
        data.get(CONF_LIVE, True),
    )


def tokens_of(spec: Spec) -> tuple[str, ...]:
    return tuple(state_token(state) for state in spec.states)


def statistic_ids(spec: Spec, existing: Mapping[str, str], metric: str) -> list[str]:
    """The entity's statistics of one metric for the spec's states - all when none."""
    wanted = tokens_of(spec)
    return [
        statistic_id
        for statistic_id in existing
        if (parts := parse(statistic_id)) is not None
        and parts[2] == metric
        and (not wanted or parts[1] in wanted)
    ]


def _source_metric(spec: Spec) -> str:
    return METRIC_COUNT if spec.metric == METRIC_COUNT else METRIC_DURATION


def _span(
    spec: Spec, frame: Frame, now: float, tz: tzinfo
) -> tuple[float | None, float, float | None]:
    """(start, end, lts_end): the period, and where the compiled part of it ends."""
    start, end = bounds(spec.period, now, tz)
    if start is None:
        start = frame.series_start
    if start is None or frame.watermark_end is None:
        return start, end, None
    return start, end, min(end, frame.watermark_end)


def edges(spec: Spec, frame: Frame, now: float, tz: tzinfo) -> set[float]:
    """The edges whose sums `compute` will ask for."""
    start, _, lts_end = _span(spec, frame, now, tz)
    if start is None or lts_end is None or lts_end <= start:
        return set()
    return {start, lts_end}


def compute(
    cfg: EntityConfig,
    spec: Spec,
    frame: Frame,
    sum_at: Callable[[str, float], float],
    timeline: Timeline | None,
    now: float,
    tz: tzinfo,
) -> Reading:
    """The sensor's value as of now.

    The statistics answer [start, min(end, watermark end)); the tail
    answers the rest up to now for a live sensor, and nothing otherwise.
    Rounded to what the display shows, so a tick where nothing changed
    writes nothing to the recorder.
    """
    start, end, lts_end = _span(spec, frame, now, tz)
    period_end = None if end == float("inf") else end
    if start is None or lts_end is None:
        return Reading(None, start, period_end, None)

    ids = statistic_ids(spec, frame.existing, _source_metric(spec))
    if spec.states and not ids and all(cfg.resolve(s) is None for s in spec.states):
        return Reading(None, start, period_end, REASON_NOT_RECORDED)

    compiled = (
        sum(sum_at(sid, lts_end) - sum_at(sid, start) for sid in ids)
        if lts_end > start
        else 0.0
    )

    live = spec.live and end > (frame.watermark_end or 0.0)
    tail_seconds, tail_count = 0.0, 0
    if live and timeline is not None:
        wanted = tokens_of(spec)
        for state, (seconds, count) in tally(
            timeline.carried,
            timeline.transitions,
            max(start, timeline.start),
            min(end, now),
        ).items():
            if not wanted or state_token(state) in wanted:
                tail_seconds += seconds
                tail_count += count

    if spec.metric == METRIC_COUNT:
        return Reading(round(compiled) + tail_count, start, period_end, None)

    hours = compiled + tail_seconds / HOUR
    if spec.metric == METRIC_DURATION:
        return Reading(round(hours, 2), start, period_end, None)

    # Share: of the time actually read - up to now when live, else up to
    # the watermark - and from the series start when the period reaches
    # back before it, since before then the state had no time to be in.
    as_of = min(end, now) if live else lts_end
    since = start if frame.series_start is None else max(start, frame.series_start)
    elapsed = as_of - since
    if elapsed <= 0:
        return Reading(None, start, period_end, None)
    return Reading(round(hours * HOUR / elapsed * 100, 1), start, period_end, None)


def suggested_entity_id(entity_id: str, spec: Spec) -> str:
    """`sensor.discrete_<entity slug>_<state tokens>_<metric>_<period>`.

    The tokens are the statistic's own, so the sensor and the series it
    reads visibly match; `all` for a sensor over every state. A prefix
    rather than a suffix, since `_this_month` is anyone's - one
    `entity_globs` exclude, `sensor.discrete_*`, covers them all.
    """
    states = "_".join(tokens_of(spec)) or "all"
    return (
        f"sensor.discrete_{slugify(entity_id, separator='_')}"
        f"_{states}_{spec.metric}_{spec.period}"
    )
