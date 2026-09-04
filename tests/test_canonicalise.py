"""Tests for turning recorder history into canonical transitions."""

from dataclasses import dataclass

from custom_components.discrete_statistics.canonicalise import canonicalise
from custom_components.discrete_statistics.config import EntityConfig

T0 = 1767225600.0


@dataclass
class FakeState:
    state: str
    last_changed_timestamp: float


def cfg(default="record_known", states=None):
    return EntityConfig(
        entity_id="binary_sensor.grid_status",
        name=None,
        default=default,
        states=states or {},
    )


def test_row_exactly_at_window_start_is_a_transition_not_carried():
    # Only a row strictly before window_start can be the carried state. A row
    # landing exactly on the boundary is a real event, so it is emitted as a
    # transition and counted once — whichever window covers it.
    carried, transitions = canonicalise(
        cfg(), [FakeState("on", T0), FakeState("off", T0 + 60.0)], T0
    )
    assert carried is None
    assert transitions == [(T0, "on"), (T0 + 60.0, "off")]


def test_rows_before_window_start_set_the_carried_state():
    carried, transitions = canonicalise(
        cfg(), [FakeState("on", T0 - 500.0), FakeState("off", T0 + 60.0)], T0
    )
    assert carried == "on"
    assert transitions == [(T0 + 60.0, "off")]


def test_no_rows_yields_no_carried_state():
    carried, transitions = canonicalise(cfg(), [], T0)
    assert carried is None
    assert transitions == []


def test_ignored_states_do_not_produce_transitions():
    carried, transitions = canonicalise(
        cfg(),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unavailable", T0 + 100.0),
            FakeState("on", T0 + 200.0),
        ],
        T0,
    )
    assert carried == "on"
    assert transitions == []


def test_ignored_state_does_not_break_a_real_transition():
    carried, transitions = canonicalise(
        cfg(),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unavailable", T0 + 100.0),
            FakeState("off", T0 + 200.0),
        ],
        T0,
    )
    assert carried == "on"
    assert transitions == [(T0 + 200.0, "off")]


def test_consecutive_duplicates_are_collapsed():
    carried, transitions = canonicalise(
        cfg(states={"cool": "cooling"}),
        [
            FakeState("cooling", T0 - 500.0),
            FakeState("cool", T0 + 100.0),
            FakeState("cooling", T0 + 200.0),
            FakeState("idle", T0 + 300.0),
        ],
        T0,
    )
    assert carried == "cooling"
    assert transitions == [(T0 + 300.0, "idle")]


def test_leading_ignored_rows_leave_carried_state_none():
    carried, transitions = canonicalise(
        cfg(),
        [FakeState("unknown", T0), FakeState("on", T0 + 100.0)],
        T0,
    )
    assert carried is None
    assert transitions == [(T0 + 100.0, "on")]




def short_cfg(default="record_known", states=None, min_duration=60.0):
    return EntityConfig(
        entity_id="binary_sensor.grid_status",
        name=None,
        default=default,
        states=states if states is not None else {"unavailable": "ignore_short"},
        min_duration=min_duration,
    )


def test_a_short_spell_is_carried_across():
    carried, transitions = canonicalise(
        short_cfg(),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unavailable", T0 + 100.0),
            FakeState("off", T0 + 130.0),
        ],
        T0,
    )
    assert carried == "on"
    assert transitions == [(T0 + 130.0, "off")]


def test_a_long_enough_spell_is_recorded():
    carried, transitions = canonicalise(
        short_cfg(),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unavailable", T0 + 100.0),
            FakeState("off", T0 + 160.0),
        ],
        T0,
    )
    assert carried == "on"
    assert transitions == [(T0 + 100.0, "unavailable"), (T0 + 160.0, "off")]


def test_a_short_spell_between_two_of_the_same_state_is_no_event():
    carried, transitions = canonicalise(
        short_cfg(),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unavailable", T0 + 100.0),
            FakeState("on", T0 + 110.0),
            FakeState("off", T0 + 300.0),
        ],
        T0,
    )
    assert carried == "on"
    assert transitions == [(T0 + 300.0, "off")]


def test_a_spell_is_measured_to_the_next_row_even_when_that_spell_is_dropped():
    """Each spell is judged on its own length, not on the run of short
    spells it sits in: under `default: ignore_short` every spell is
    conditional, and the run would be the whole timeline."""
    carried, transitions = canonicalise(
        short_cfg(states={"unavailable": "ignore_short", "unknown": "ignore_short"}),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unavailable", T0 + 100.0),
            FakeState("unknown", T0 + 140.0),
            FakeState("off", T0 + 180.0),
        ],
        T0,
    )
    assert carried == "on"
    assert transitions == [(T0 + 180.0, "off")]


def test_a_spell_still_open_at_the_last_row_is_kept_unless_known_to_be_short():
    rows = [FakeState("on", T0 - 500.0), FakeState("unavailable", T0 + 100.0)]
    assert canonicalise(short_cfg(), rows, T0) == (
        "on",
        [(T0 + 100.0, "unavailable")],
    )
    assert canonicalise(short_cfg(), rows, T0, known_until=T0 + 130.0) == (
        "on",
        [],
    )
    assert canonicalise(short_cfg(), rows, T0, known_until=T0 + 160.0) == (
        "on",
        [(T0 + 100.0, "unavailable")],
    )


def test_rows_past_the_window_end_measure_spells_but_are_not_transitions():
    carried, transitions = canonicalise(
        short_cfg(),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unavailable", T0 + 3590.0),
            FakeState("off", T0 + 3660.0),
        ],
        T0,
        T0 + 3600.0,
    )
    assert carried == "on"
    assert transitions == [(T0 + 3590.0, "unavailable")]


def test_a_short_spell_before_the_window_does_not_become_the_carried_state():
    carried, transitions = canonicalise(
        short_cfg(),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unavailable", T0 - 20.0),
            FakeState("off", T0 + 20.0),
        ],
        T0,
    )
    assert carried == "on"
    assert transitions == [(T0 + 20.0, "off")]


def test_a_short_spell_straddling_the_window_start_gives_the_hour_to_the_state_before_it():
    carried, transitions = canonicalise(
        short_cfg(),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unavailable", T0 - 20.0),
            FakeState("on", T0 + 20.0),
        ],
        T0,
    )
    assert carried == "on"
    assert transitions == []


def test_ignore_short_as_the_default_debounces_a_bouncing_contact():
    carried, transitions = canonicalise(
        short_cfg(default="ignore_short", states={}, min_duration=5.0),
        [
            FakeState("off", T0 - 500.0),
            FakeState("on", T0 + 100.0),
            FakeState("off", T0 + 100.2),
            FakeState("on", T0 + 100.4),
            FakeState("off", T0 + 100.6),
            FakeState("on", T0 + 200.0),
            FakeState("off", T0 + 260.0),
        ],
        T0,
    )
    assert carried == "off"
    assert transitions == [(T0 + 200.0, "on"), (T0 + 260.0, "off")]


def test_a_spell_is_conditional_only_if_every_row_in_it_is():
    """`x` maps to `unknown` unconditionally; `unknown` itself is
    conditional. A spell holding both was entered unconditionally."""
    carried, transitions = canonicalise(
        short_cfg(states={"x": "unknown", "unknown": "ignore_short"}),
        [
            FakeState("on", T0 - 500.0),
            FakeState("unknown", T0 + 100.0),
            FakeState("x", T0 + 110.0),
            FakeState("on", T0 + 120.0),
        ],
        T0,
    )
    assert carried == "on"
    assert transitions == [(T0 + 100.0, "unknown"), (T0 + 120.0, "on")]
