"""Pure bucketing of state transitions into hourly (seconds, count) pairs.

No Home Assistant imports: this module is the algorithmic core and is
tested in isolation.
"""

from __future__ import annotations

import math

from .const import HOUR

BucketKey = tuple[str, float]
BucketValue = tuple[float, int]


def hour_start(timestamp: float) -> float:
    """Return the start of the UTC hour containing timestamp."""
    return math.floor(timestamp / HOUR) * HOUR


def first_whole_hour(timestamp: float) -> float:
    """Return the first hour boundary at or after timestamp.

    The hour containing a timestamp starts before it, so an hour opened
    there would be only partly known - and a part-known hour cannot both be
    recorded and total wall-clock time. Both callers pay the partial hour
    rather than misattribute it.
    """
    opening = hour_start(timestamp)
    return opening if opening == timestamp else opening + HOUR


def bucket(
    carried_state: str,
    transitions: list[tuple[float, str]],
    window_start: float,
    window_end: float,
) -> dict[BucketKey, BucketValue]:
    """Bucket a run of states into {(state, hour_start): (seconds, count)}.

    carried_state is the canonical state in effect at window_start. The
    compiler never buckets a window it cannot open in a known state, so
    there is no unknown to attribute.

    transitions must be ascending by timestamp, contain no two consecutive
    entries with the same state, and hold canonical state names only.
    Transitions outside [window_start, window_end) are ignored. One
    landing exactly on window_start contributes zero seconds and one
    count, so a boundary transition is counted once whichever window
    covers it.

    Durations are split at hour boundaries so that the total seconds
    returned always equals window_end - window_start. Counts are attributed
    to the hour containing the transition.
    """
    result: dict[BucketKey, BucketValue] = {}

    def add_duration(state: str, start: float, end: float) -> None:
        while start < end:
            hour = hour_start(start)
            edge = min(hour + HOUR, end)
            seconds, count = result.get((state, hour), (0.0, 0))
            result[(state, hour)] = (seconds + (edge - start), count)
            start = edge

    def add_count(state: str, timestamp: float) -> None:
        hour = hour_start(timestamp)
        seconds, count = result.get((state, hour), (0.0, 0))
        result[(state, hour)] = (seconds, count + 1)

    current = carried_state
    cursor = window_start

    for timestamp, state in transitions:
        if timestamp < window_start:
            continue
        if timestamp >= window_end:
            break
        add_duration(current, cursor, timestamp)
        add_count(state, timestamp)
        current = state
        cursor = timestamp

    add_duration(current, cursor, window_end)
    return result


def tally(
    carried: str | None,
    transitions: list[tuple[float, str]],
    start: float,
    end: float,
) -> dict[str, tuple[float, int]]:
    """Seconds and transitions per state over [start, end), summed over hours.

    The live tail of a period sensor: `bucket` over the same rules the
    compiler writes by, then folded per state. A count is the number of
    transitions *into* that state inside [start, end) - a spell already in
    progress when the window opens is not a transition, so it contributes
    its seconds and no count.

    Transitions before `start` move the carried state along rather than
    counting, as `canonicalise` folds rows before a window into the state
    carried into it. A carried state of None is a timeline that opens in
    no known state: the window opens at its first transition instead,
    which `bucket` then counts as the boundary transition it is.
    """
    if end <= start:
        return {}
    index = 0
    while index < len(transitions) and transitions[index][0] < start:
        carried = transitions[index][1]
        index += 1
    if carried is None:
        if index == len(transitions):
            return {}
        start, carried = transitions[index]
        if end <= start:
            return {}
    result: dict[str, tuple[float, int]] = {}
    for (state, _), (seconds, count) in bucket(
        carried, transitions[index:], start, end
    ).items():
        had_seconds, had_count = result.get(state, (0.0, 0))
        result[state] = (had_seconds + seconds, had_count + count)
    return result
