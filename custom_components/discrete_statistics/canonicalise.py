"""Turn recorder history rows into canonical state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import EntityConfig


class StateLike(Protocol):
    """The parts of a recorder State this module uses."""

    state: str
    last_changed_timestamp: float


@dataclass(slots=True)
class _Spell:
    """A run of rows in one canonical state."""

    start: float
    state: str
    # `ignore_short`: kept only if the spell is long enough.
    short: bool


def canonicalise(
    cfg: EntityConfig,
    states: list[StateLike],
    window_start: float,
    window_end: float = float("inf"),
    known_until: float | None = None,
) -> tuple[str | None, list[tuple[float, str]]]:
    """Return (carried_state, transitions) for the bucketer.

    `states` MUST be ascending by last_changed_timestamp. Out-of-order rows
    are not detected and silently produce wrong output.

    carried_state is the canonical state in effect at window_start, or None
    if no recordable state precedes it. Only a row strictly before
    window_start can set it: a row landing exactly on window_start is a
    transition, so that the same event is counted once no matter which
    window covers it.

    Transitions are ascending, in [window_start, window_end), and contain
    no two consecutive entries with the same canonical state. Ignored raw
    states produce no transition, so the previous state simply continues.

    So does a spell of an `ignore_short` state shorter than
    `cfg.min_duration`. A spell is measured to the next recordable row,
    whether or not that row's own spell survives, and rows past
    `window_end` serve as ends for the spells before them - which is why
    the window's end is not where the rows have to stop. A spell still
    open at the last row is measured to `known_until`, and kept when there
    is none: a spell that has not ended yet cannot be called short.
    """
    spells: list[_Spell] = []
    for row in states:
        canonical, short = cfg.classify(row.state)
        if canonical is None:
            continue
        if spells and spells[-1].state == canonical:
            spells[-1].short &= short
            continue
        spells.append(_Spell(row.last_changed_timestamp, canonical, short))

    kept: list[_Spell] = []
    for index, spell in enumerate(spells):
        if spell.short:
            if index + 1 < len(spells):
                end = spells[index + 1].start
            elif known_until is not None:
                end = known_until
            else:
                end = None
            if end is not None and end - spell.start < cfg.min_duration:
                continue
        if kept and kept[-1].state == spell.state:
            continue  # the state it never left, as far as we record
        kept.append(spell)

    carried: str | None = None
    transitions: list[tuple[float, str]] = []
    for spell in kept:
        if spell.start < window_start:
            carried = spell.state
        elif spell.start < window_end:
            transitions.append((spell.start, spell.state))

    return carried, transitions
