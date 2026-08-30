"""Turn recorder history rows into canonical state transitions."""

from __future__ import annotations

from typing import Protocol

from .config import EntityConfig


class StateLike(Protocol):
    """The parts of a recorder State this module uses."""

    state: str
    last_changed_timestamp: float


def canonicalise(
    cfg: EntityConfig,
    states: list[StateLike],
    window_start: float,
) -> tuple[str | None, list[tuple[float, str]]]:
    """Return (carried_state, transitions) for the bucketer.

    carried_state is the canonical state in effect at window_start, or None
    if no recordable state precedes it.

    Transitions are ascending, strictly after window_start, and contain no
    two consecutive entries with the same canonical state. Ignored raw
    states produce no transition, so the previous state simply continues.
    """
    carried: str | None = None
    transitions: list[tuple[float, str]] = []
    current: str | None = None

    for row in states:
        canonical = cfg.resolve(row.state)
        if canonical is None:
            continue
        if canonical == current:
            continue

        if row.last_changed_timestamp <= window_start:
            carried = canonical
        else:
            transitions.append((row.last_changed_timestamp, canonical))
        current = canonical

    return carried, transitions
