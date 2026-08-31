"""Pure bucketing of state transitions into hourly (seconds, count) pairs.

No Home Assistant imports: this module is the algorithmic core and is
tested in isolation.
"""

from __future__ import annotations

import math

from .const import HOUR, NO_DATA

BucketKey = tuple[str, float]
BucketValue = tuple[float, int]


def hour_start(timestamp: float) -> float:
    """Return the start of the UTC hour containing timestamp."""
    return math.floor(timestamp / HOUR) * HOUR


def bucket(
    carried_state: str | None,
    transitions: list[tuple[float, str]],
    window_start: float,
    window_end: float,
) -> dict[BucketKey, BucketValue]:
    """Bucket a run of states into {(state, hour_start): (seconds, count)}.

    carried_state is the canonical state in effect at window_start, or None
    if it is unknown, in which case the span until the first transition is
    attributed to NO_DATA.

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

    def add_seconds(state: str, start: float, end: float) -> None:
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

    current = carried_state if carried_state is not None else NO_DATA
    cursor = window_start

    for timestamp, state in transitions:
        if timestamp < window_start:
            continue
        if timestamp >= window_end:
            break
        add_seconds(current, cursor, timestamp)
        add_count(state, timestamp)
        current = state
        cursor = timestamp

    add_seconds(current, cursor, window_end)
    return result
