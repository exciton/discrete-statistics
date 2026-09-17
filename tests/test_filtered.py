"""The live tracker: raw states in, the state the entry records out.

The last test is the one that matters most. `Tracker` and `canonicalise`
answer the same question from opposite ends - one row at a time with no
future to look at, and a whole list at once - and the only thing keeping
them from drifting apart is that they must agree over the same rows.
"""

from dataclasses import dataclass

import pytest

from custom_components.discrete_statistics.canonicalise import canonicalise
from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.filtered import (
    REMOVED,
    Tracker,
    suggested_entity_id,
)

T0 = 1767225600.0


@dataclass
class FakeState:
    state: str
    last_changed_timestamp: float


def cfg(default="record_known", states=None, min_duration=0.0, blank="unknown"):
    return EntityConfig(
        entity_id="binary_sensor.grid_status",
        name=None,
        default=default,
        states=states or {},
        blank=blank,
        min_duration=min_duration,
    )


def track(config, rows, carried=None):
    """Feed rows to a tracker in order and return it."""
    tracker = Tracker(config, carried)
    for state, when in rows:
        tracker.observe(state, when)
    return tracker


def test_nothing_seen_is_none_not_a_state():
    # Unknown, not Unavailable: the entity may be perfectly healthy and
    # simply sitting in a state this entry ignores.
    assert Tracker(cfg()).state(T0) is None


def test_a_recorded_state_is_reported():
    tracker = track(cfg(), [("on", T0)])
    assert tracker.state(T0 + 1) == "on"


@pytest.mark.parametrize("ignored", ["unavailable", "unknown"])
def test_record_known_never_reports_an_ignored_state(ignored):
    tracker = track(cfg(), [("on", T0), (ignored, T0 + 60.0)])
    assert tracker.state(T0 + 120.0) == "on"


def test_a_removed_entity_is_carried_across():
    # `new_state=None` reaches the tracker as the blank state, which
    # `blank:` turns into `unknown` and `record_known` then ignores. This
    # is the dev-tools YAML reload: the entity goes and comes back.
    tracker = track(cfg(), [("on", T0), (None, T0 + 60.0)])
    assert tracker.state(T0 + 120.0) == "on"
    tracker.observe("unavailable", T0 + 121.0)
    tracker.observe("unknown", T0 + 122.0)
    assert tracker.state(T0 + 130.0) == "on"
    tracker.observe("off", T0 + 140.0)
    assert tracker.state(T0 + 150.0) == "off"


def test_removal_is_recorded_when_blank_names_a_recorded_state():
    # The other half of the same rule: a blank state is only ignored
    # because `blank:` and the default say so, not because it is blank.
    tracker = track(cfg(blank="gone", states={"gone": "record"}), [("on", T0)])
    tracker.observe(None, T0 + 60.0)
    assert tracker.state(T0 + 61.0) == "gone"


def test_removal_and_the_empty_string_are_the_same_state():
    config = cfg(blank="gone", states={"gone": "record"})
    assert track(config, [(None, T0)]).state(T0) == "gone"
    assert track(config, [(REMOVED, T0)]).state(T0) == "gone"


def test_a_map_target_is_what_is_reported():
    tracker = track(cfg(states={"cool": "cooling"}), [("cool", T0)])
    assert tracker.state(T0) == "cooling"


def test_unavailable_as_a_map_target_is_recorded_despite_record_known():
    # A map target is recorded whatever the default says, so this entry
    # really does record `unavailable` - and the sensor must agree with
    # the statistics rather than with the general rule.
    config = cfg(states={"offline": "unavailable"})
    assert (
        track(config, [("on", T0), ("unavailable", T0 + 60.0)]).state(T0 + 61.0)
        == "unavailable"
    )


def test_a_short_spell_is_carried_until_it_has_lasted_long_enough():
    config = cfg(default="ignore_short", min_duration=5.0)
    tracker = track(config, [("on", T0), ("off", T0 + 10.0)])
    # The `off` spell is two seconds old: too soon to record, so `on`
    # still stands - the provisional verdict a compile of this hour would
    # reach too.
    assert tracker.state(T0 + 12.0) == "on"
    assert tracker.state(T0 + 15.0) == "off"


def test_pending_until_is_when_the_answer_would_change():
    config = cfg(default="ignore_short", min_duration=5.0)
    tracker = track(config, [("on", T0), ("off", T0 + 10.0)])
    assert tracker.pending_until(T0 + 12.0) == T0 + 15.0
    # Once it has matured there is nothing left to wait for.
    assert tracker.pending_until(T0 + 15.0) is None


def test_nothing_is_pending_without_ignore_short():
    tracker = track(cfg(), [("on", T0)])
    assert tracker.pending_until(T0 + 1.0) is None


def test_a_bounce_never_reaches_the_answer():
    # off, on, off inside the threshold: the door closed once.
    config = cfg(default="ignore_short", min_duration=5.0)
    tracker = track(config, [("off", T0), ("on", T0 + 10.0), ("off", T0 + 11.0)])
    assert tracker.state(T0 + 30.0) == "off"


def test_each_spell_is_judged_on_its_own_length():
    # Two short spells, not one long one: neither survives, so the state
    # before them carries the whole way.
    config = cfg(default="ignore_short", min_duration=5.0)
    tracker = track(
        config,
        [("idle", T0), ("on", T0 + 10.0), ("off", T0 + 12.0)],
    )
    assert tracker.state(T0 + 13.0) == "idle"


def test_a_spell_keeps_its_start_across_rows_of_the_same_canonical_state():
    # `heat` and `cool` both map to `active`, so the second row continues
    # the spell rather than starting one; the threshold is measured from
    # the first.
    config = cfg(
        default="ignore_short",
        states={"heat": "active", "cool": "active"},
        min_duration=5.0,
    )
    tracker = track(config, [("idle", T0), ("heat", T0 + 10.0)])
    tracker.observe("cool", T0 + 12.0)
    assert tracker.state(T0 + 16.0) == "active"


def test_an_ignored_row_does_not_end_the_spell_in_progress():
    # A door bouncing while the device also flickers offline. The
    # `unavailable` row is ignored, so it is not a row at all: the `on`
    # spell runs through it and matures, rather than being cut short at
    # one second and dropped.
    config = cfg(
        default="ignore_short",
        states={"unavailable": "ignore"},
        min_duration=5.0,
    )
    tracker = track(config, [("off", T0), ("on", T0 + 10.0)])
    tracker.observe("unavailable", T0 + 11.0)
    assert tracker.state(T0 + 12.0) == "off"
    assert tracker.state(T0 + 16.0) == "on"


def test_one_unconditional_row_clears_the_short_flag_for_the_whole_spell():
    # `on` is conditional; `activated` maps onto it, and a map target is
    # always recorded. One row of the second kind makes the spell
    # recordable, however short it still is - which is `canonicalise`'s
    # `short &= short` over a spell's rows, and the reason the tracker
    # keeps a second row at all.
    config = cfg(
        default="ignore",
        states={"off": "record", "on": "ignore_short", "activated": "on"},
        min_duration=60.0,
    )
    tracker = track(config, [("off", T0), ("on", T0 + 10.0)])
    assert tracker.state(T0 + 11.0) == "off"
    tracker.observe("activated", T0 + 12.0)
    assert tracker.state(T0 + 13.0) == "on"


def test_settings_that_ignore_everything_report_nothing():
    assert track(cfg(default="ignore"), [("on", T0)]).state(T0) is None


def test_the_carried_state_opens_the_tracker():
    # What a restore hands back, and what survives a settings change.
    tracker = track(cfg(), [("unavailable", T0)], carried="off")
    assert tracker.state(T0 + 1.0) == "off"


def test_suggested_entity_id_is_outside_the_period_sensors_prefix():
    assert (
        suggested_entity_id("binary_sensor.grid_status")
        == "sensor.filtered_discrete_binary_sensor_grid_status"
    )
    assert not suggested_entity_id("binary_sensor.grid_status").startswith(
        "sensor.discrete_"
    )


# Raw states drawn so that several of them collide on one canonical state,
# some of them conditional and some not, which is where the two
# implementations have the most room to disagree.
TIMELINES = [
    [("on", 0.0), ("off", 30.0), ("on", 90.0)],
    [("on", 0.0), ("unavailable", 10.0), ("on", 12.0), ("off", 200.0)],
    [("on", 0.0), ("", 10.0), ("unknown", 20.0), ("off", 400.0)],
    [("heat", 0.0), ("cool", 3.0), ("idle", 5.0), ("heat", 500.0)],
    [("off", 0.0), ("on", 1.0), ("off", 2.0), ("on", 3.0), ("off", 4.0)],
    [("unavailable", 0.0), ("unknown", 1.0), ("on", 2.0)],
    [("on", 0.0), ("on", 50.0), ("off", 100.0), ("off", 150.0)],
    [("off", 0.0), ("on", 10.0), ("activated", 12.0), ("off", 300.0)],
    # Ends inside the spell, so the verdict is still provisional at `now`.
    [("off", 0.0), ("on", 100.0), ("activated", 102.0)],
    [("on", 0.0), ("off", 100.0)],
    # An ignored row landing inside a spell that has not matured yet: the
    # spell must run through it rather than be cut off at it.
    [("off", 0.0), ("on", 10.0), ("unavailable", 11.0)],
    [("off", 0.0), ("on", 10.0), ("unavailable", 11.0), ("", 12.0)],
]

CONFIGS = [
    cfg(),
    cfg(default="record"),
    cfg(default="ignore_short", min_duration=5.0),
    cfg(default="ignore_short_unknown", min_duration=5.0),
    cfg(states={"heat": "active", "cool": "active"}),
    cfg(
        default="ignore_short",
        states={"heat": "active", "cool": "active", "unavailable": "ignore"},
        min_duration=5.0,
    ),
    cfg(blank="gone", states={"gone": "record"}),
    cfg(
        default="ignore",
        states={"off": "record", "on": "ignore_short", "activated": "on"},
        min_duration=60.0,
    ),
    cfg(
        default="ignore_short",
        states={"unavailable": "ignore"},
        min_duration=5.0,
    ),
]


@pytest.mark.parametrize("config", CONFIGS, ids=range(len(CONFIGS)))
@pytest.mark.parametrize("timeline", TIMELINES, ids=range(len(TIMELINES)))
@pytest.mark.parametrize("now_offset", [0.0, 2.0, 600.0])
def test_the_tracker_agrees_with_canonicalise_over_the_same_rows(
    config, timeline, now_offset
):
    """The anti-drift test.

    `canonicalise` is what a compile resolves an hour with, so whatever it
    says the entity was in last at `now` is what the statistics will show
    - and therefore what this sensor has to be reading. Feeding the same
    rows to both is the only thing that keeps the streaming form honest
    when either side is changed.
    """
    rows = [(state, T0 + when) for state, when in timeline]
    now = T0 + timeline[-1][1] + now_offset

    tracker = track(config, rows)

    carried, transitions = canonicalise(
        config,
        [FakeState(state, when) for state, when in rows],
        window_start=rows[0][1],
        known_until=now,
    )
    expected = transitions[-1][1] if transitions else carried

    assert tracker.state(now) == expected
