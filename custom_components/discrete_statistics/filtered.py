"""The state the entity is in now, as the compiler would resolve it.

Pure: raw states in, one canonical state out. The disposition table is
never read here - `EntityConfig.classify` applies it and `canonicalise`
decides what becomes of a spell, which is what keeps this and the compile
path from ever disagreeing about what a state means.

A compile answers "what state was it in" from recorder rows, with the next
row always to hand to measure a spell against. Live there is no next row:
a spell of an `ignore_short` state that started two seconds ago may yet
become recordable, and nothing will arrive to say so. `Tracker` therefore
keeps the spell in progress rather than a verdict, and answers against a
`now` the caller passes in - so the same tracker reads one way before the
spell matures and another after it, with no row in between. `pending_until`
is when that flip is due, for the caller to set a timer by.

The spell in progress is held as at most two rows because `canonicalise`
reads exactly two things off a spell: the timestamp of its first row, and
whether *every* row in it was `ignore_short`. The first row carries the
one, and the first row to arrive that is not `ignore_short` settles the
other; a third row could change neither.
"""

from __future__ import annotations

import math
from typing import NamedTuple

from homeassistant.util import slugify

from .canonicalise import canonicalise
from .config import EntityConfig

# The state a removed entity leaves behind. `hass.states.async_remove`
# hands a listener `new_state=None`, the recorder stores NULL for it, and
# the history API reads that back as "" - so a compile covering the same
# moment sees a blank state, and `blank:` decides what becomes of it.
# Translating here rather than at the caller is what makes the two agree.
REMOVED = ""


class Change(NamedTuple):
    """One raw state and when it arrived - `canonicalise`'s StateLike."""

    state: str
    last_changed_timestamp: float


class Tracker:
    """The canonical state of one entity, kept up to date row by row.

    Built with the state carried in from before the first row - a restored
    value, or None when nothing is known yet. `state(now)` is the answer at
    that instant; None means no recordable state has ever been seen, which
    is Unknown rather than Unavailable: the entity may be perfectly healthy
    and simply sitting in a state this entry ignores.
    """

    def __init__(self, cfg: EntityConfig, carried: str | None = None) -> None:
        self._cfg = cfg
        self._carried = carried
        # The spell in progress: its canonical state, and the rows that
        # decide its verdict.
        self._state: str | None = None
        self._rows: list[Change] = []

    def observe(self, raw_state: str | None, when: float) -> None:
        """Take one raw state, at the moment it took effect.

        `None` is an entity that has gone from the state machine, which is
        `REMOVED`. An ignored state is not a row at all: the spell in
        progress simply continues, which is the carry-forward the whole
        disposition table is written in terms of.
        """
        raw = REMOVED if raw_state is None else raw_state
        canonical, short = self._cfg.classify(raw)
        if canonical is None:
            return
        if canonical == self._state:
            # See the module docstring: only the first row that is not
            # `ignore_short` can still change this spell's verdict.
            if not short and len(self._rows) < 2:
                self._rows.append(Change(raw, when))
            return
        # A new spell begins, so the one before it has ended here and can
        # be judged on its final length.
        self._settle(when)
        self._state = canonical
        self._rows = [Change(raw, when)]

    def state(self, now: float) -> str | None:
        """The canonical state at `now`, or None if none is known yet."""
        return self._resolve(now)

    def pending_until(self, now: float) -> float | None:
        """When the spell in progress would change the answer, or None.

        A short spell is carried across until it has lasted `min_duration`.
        Nothing arrives to mark that moment - the next row is what ends a
        spell, and there may not be one for hours - so a caller that wants
        the change on time sets a timer for this.
        """
        if not self._rows or self._resolve(now) == self._resolve(math.inf):
            return None
        return self._rows[0].last_changed_timestamp + self._cfg.min_duration

    def _settle(self, end: float) -> None:
        """Fold the spell in progress into the carried state, ended at `end`."""
        self._carried = self._resolve(end)
        self._state = None
        self._rows = []

    def _resolve(self, known_until: float) -> str | None:
        """The answer with the spell in progress measured to `known_until`.

        `canonicalise` over the spell's own rows: one spell in, one
        transition out if it is recordable and none if it is too short,
        which is exactly the verdict a compile reaching this moment would
        reach. A spell that does not survive leaves the carried state
        standing, and one that resolves to the carried state replaces it
        with itself.
        """
        if not self._rows:
            return self._carried
        _, transitions = canonicalise(
            self._cfg,
            self._rows,
            self._rows[0].last_changed_timestamp,
            known_until=known_until,
        )
        return transitions[-1][1] if transitions else self._carried


def suggested_entity_id(entity_id: str) -> str:
    """`sensor.filtered_discrete_<entity slug>`.

    Deliberately outside the `sensor.discrete_` prefix the period sensors
    take. The README asks for those to be kept out of the recorder - they
    change every minute and say nothing the statistics do not - and one
    `entity_globs` exclude covers them. This sensor changes only when the
    entity's recorded state changes, so its history is worth keeping and a
    shared prefix would sweep it into the same exclude.

    No qualifier after the entity: there is one recorded state, so there is
    one of these per entry.
    """
    return f"sensor.filtered_discrete_{slugify(entity_id, separator='_')}"
