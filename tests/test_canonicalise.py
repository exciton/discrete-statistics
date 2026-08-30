"""Tests for turning recorder history into canonical transitions."""

from dataclasses import dataclass

from custom_components.discrete_stats.canonicalise import canonicalise
from custom_components.discrete_stats.config import EntityConfig

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


def test_carried_state_comes_from_the_row_at_window_start():
    carried, transitions = canonicalise(
        cfg(), [FakeState("on", T0), FakeState("off", T0 + 60.0)], T0
    )
    assert carried == "on"
    assert transitions == [(T0 + 60.0, "off")]


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
            FakeState("on", T0),
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
            FakeState("on", T0),
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
            FakeState("cooling", T0),
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


def test_source_state_named_no_data_is_ignored():
    carried, transitions = canonicalise(
        cfg(default="record"),
        [FakeState("on", T0), FakeState("no_data", T0 + 100.0)],
        T0,
    )
    assert carried == "on"
    assert transitions == []
