"""One period sensor's value, from the sums at hour edges and a live tail.

Pure: the coordinator fetches the sums, the part hours and the tail, this
module does the arithmetic. A window is read in pieces. Its whole compiled
hours are read as the card reads a bucket - the sum at the first edge
against the sum at the last. A part hour, where the window starts or ends
inside an hour, is exact when the compiler's timeline for that hour is to
hand, cut at the edge, and estimated from the hour's compiled change
otherwise - time in proportion, a count rounded to whole changes. The
hours after the watermark, not yet compiled, come from the compiler's
read path as a `Timeline` tallied per state. The pieces never overlap.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import tzinfo
from typing import TYPE_CHECKING, Any, NamedTuple

from homeassistant.util import slugify

from .bucketer import hour_start, tally
from .config import CONF_STATES, EntityConfig
from .const import (
    CONF_LIVE,
    CONF_METRIC,
    CONF_PERIOD,
    CONF_WINDOW_DURATION,
    CONF_WINDOW_END,
    CONF_WINDOW_START,
    HOUR,
    METRIC_COUNT,
    METRIC_DURATION,
)
from .periods import bounds, is_custom
from .statistic_ids import parse, state_token

if TYPE_CHECKING:
    from .compiler import Timeline

# Why a sensor is unavailable rather than zero: every state it asks for
# resolves to nothing under the entry's settings and none was ever
# recorded, so there is no series to read and the tail would never
# contribute.
REASON_NOT_RECORDED = "not recorded by this entry's settings"


class Custom(NamedTuple):
    """A custom period's window as the subentry stores it: templates, seconds."""

    start: str | None
    end: str | None
    duration: float | None


class Spec(NamedTuple):
    """What a sensor subentry asks for."""

    states: tuple[str, ...]
    metric: str
    period: str
    live: bool
    custom: Custom | None = None


class Frame(NamedTuple):
    """What the recorder holds for the entity, fetched once per refresh."""

    existing: dict[str, str]
    watermark_end: float | None
    series_start: float | None
    # The oldest retained state: a part hour after it can be read exactly.
    earliest: float | None = None


class Reading(NamedTuple):
    value: float | int | None
    period_start: float | None
    period_end: float | None
    reason: str | None
    estimated: bool = False


class Partial(NamedTuple):
    """The part of an hour inside a window."""

    hour: float
    start: float
    end: float

    @property
    def fraction(self) -> float:
        return (self.end - self.start) / HOUR


class PartialValue(NamedTuple):
    """Seconds and changes per state token inside a part hour."""

    seconds: dict[str, float]
    counts: dict[str, int]
    exact: bool


class Pieces(NamedTuple):
    """A window against the watermark: whole hours, part hours, the tail."""

    compiled: tuple[float, float] | None
    partials: tuple[Partial, ...]
    tail: tuple[float, float] | None


def spec_from(data: Mapping[str, Any]) -> Spec:
    period = data.get(CONF_PERIOD, "this_month")
    custom = (
        Custom(
            data.get(CONF_WINDOW_START) or None,
            data.get(CONF_WINDOW_END) or None,
            data.get(CONF_WINDOW_DURATION),
        )
        if is_custom(period)
        else None
    )
    return Spec(
        tuple(data.get(CONF_STATES) or ()),
        data.get(CONF_METRIC, METRIC_DURATION),
        period,
        data.get(CONF_LIVE, True),
        custom,
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


def _ceil_hour(timestamp: float) -> float:
    floor = hour_start(timestamp)
    return floor if floor == timestamp else floor + HOUR


def pieces(start: float, end: float, watermark_end: float, now: float) -> Pieces:
    """Split [start, end) into what the statistics answer and what the tail does.

    The statistics reach to the watermark end, the tail to now, and a
    window to whichever of its end and now comes first. Inside the
    statistics' reach, an edge that is not on the hour leaves a part hour
    on its side of the boundary; both edges in one hour leave one.
    """
    until = min(end, now)
    if until <= start:
        return Pieces(None, (), None)
    if start >= watermark_end:
        return Pieces(None, (), (start, until))
    compiled_until = min(until, watermark_end)
    first = _ceil_hour(start)
    last = hour_start(compiled_until)
    partials: list[Partial] = []
    if start < first:
        partials.append(Partial(hour_start(start), start, min(first, compiled_until)))
    if last < compiled_until and last >= first:
        partials.append(Partial(last, last, compiled_until))
    compiled = (first, last) if last > first else None
    tail = (max(start, watermark_end), until) if until > watermark_end else None
    return Pieces(compiled, tuple(partials), tail)


def _window(
    spec: Spec, frame: Frame, now: float, tz: tzinfo, window: tuple[float, float] | None
) -> tuple[float | None, float]:
    if is_custom(spec.period):
        if window is None:
            raise ValueError("a custom period needs its rendered window")
        return window
    start, end = bounds(spec.period, now, tz)
    return (frame.series_start if start is None else start), end


def plan(
    spec: Spec,
    frame: Frame,
    now: float,
    tz: tzinfo,
    window: tuple[float, float] | None = None,
) -> Pieces | None:
    """The pieces `compute` will read, or None when nothing is compiled yet."""
    start, end = _window(spec, frame, now, tz, window)
    if start is None or frame.watermark_end is None:
        return None
    return pieces(start, end, frame.watermark_end, now)


def edges_of(pieces_: Pieces | None) -> set[float]:
    """The edges whose sums the pieces need: the whole hours' and each part hour's."""
    if pieces_ is None:
        return set()
    edges: set[float] = set(pieces_.compiled or ())
    for partial in pieces_.partials:
        edges |= {partial.hour, partial.hour + HOUR}
    return edges


def exact_partial(partial: Partial, timeline: Timeline) -> PartialValue:
    """The hour's timeline, cut at the window's edge."""
    seconds: dict[str, float] = {}
    counts: dict[str, int] = {}
    for state, (state_seconds, count) in tally(
        timeline.carried, timeline.transitions, partial.start, partial.end
    ).items():
        token = state_token(state)
        seconds[token] = seconds.get(token, 0.0) + state_seconds
        counts[token] = counts.get(token, 0) + count
    return PartialValue(seconds, counts, True)


def prorate(
    partial: Partial, seconds: Mapping[str, float], counts: Mapping[str, float]
) -> PartialValue:
    """The hour's compiled change, scaled by the part of it inside the window.

    Counts are rounded half up: one change in an hour is a whole change
    when at least half the hour is inside the window, and none otherwise.
    """
    fraction = partial.fraction
    return PartialValue(
        {token: value * fraction for token, value in seconds.items()},
        {token: math.floor(count * fraction + 0.5) for token, count in counts.items()},
        False,
    )


def compute(
    cfg: EntityConfig,
    spec: Spec,
    frame: Frame,
    sum_at: Callable[[str, float], float],
    partial_at: Callable[[Partial], PartialValue | None],
    timeline: Timeline | None,
    now: float,
    tz: tzinfo,
    window: tuple[float, float] | None = None,
) -> Reading:
    """The sensor's value as of now.

    The whole compiled hours come from the sums at their edges, each part
    hour from `partial_at` - the coordinator's exact or estimated answer,
    or None for an hour nobody can speak for - and the tail from the
    timeline, for a live sensor. Rounded to what the display shows, so a
    tick where nothing changed writes nothing to the recorder.
    """
    start, end = _window(spec, frame, now, tz, window)
    period_end = None if end == math.inf else end
    if start is None or frame.watermark_end is None:
        return Reading(None, start, period_end, None)

    ids = statistic_ids(spec, frame.existing, _source_metric(spec))
    if spec.states and not ids and all(cfg.resolve(s) is None for s in spec.states):
        return Reading(None, start, period_end, REASON_NOT_RECORDED)

    wanted = tokens_of(spec)

    def counted(token: str) -> bool:
        return not wanted or token in wanted

    parts = pieces(start, end, frame.watermark_end, now)
    compiled = 0.0
    if parts.compiled is not None:
        first, last = parts.compiled
        compiled = sum(sum_at(sid, last) - sum_at(sid, first) for sid in ids)

    seconds, count, estimated = 0.0, 0, False
    for partial in parts.partials:
        if (value := partial_at(partial)) is None:
            continue
        estimated = estimated or not value.exact
        seconds += sum(s for token, s in value.seconds.items() if counted(token))
        count += sum(c for token, c in value.counts.items() if counted(token))

    live = spec.live and parts.tail is not None
    if live and timeline is not None:
        tail_start, tail_end = parts.tail
        for state, (state_seconds, state_count) in tally(
            timeline.carried,
            timeline.transitions,
            max(tail_start, timeline.start),
            tail_end,
        ).items():
            if counted(state_token(state)):
                seconds += state_seconds
                count += state_count

    if spec.metric == METRIC_COUNT:
        return Reading(round(compiled) + count, start, period_end, None, estimated)

    hours = compiled + seconds / HOUR
    if spec.metric == METRIC_DURATION:
        return Reading(round(hours, 2), start, period_end, None, estimated)

    # Share: of the time actually read - up to now when live, else up to
    # the watermark - and from the series start when the period reaches
    # back before it, since before then the state had no time to be in.
    as_of = parts.tail[1] if live else min(end, now, frame.watermark_end)
    since = start if frame.series_start is None else max(start, frame.series_start)
    elapsed = as_of - since
    if elapsed <= 0:
        return Reading(None, start, period_end, None, estimated)
    return Reading(
        round(hours * HOUR / elapsed * 100, 1), start, period_end, None, estimated
    )


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
