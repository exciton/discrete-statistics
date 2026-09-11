"""The compiler's read path handed out as a timeline, never written."""

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.discrete_statistics.bucketer import tally
from custom_components.discrete_statistics.compiler import (
    Compiler,
    Timeline,
    compiled_signal,
)
from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import HOUR
from tests.test_compiler import existing, read_sums

ENTITY = "binary_sensor.grid_status"
ON_DURATION = "discrete_statistics:binary_sensor_grid_status_on_duration"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def cfg(default="record_known", min_duration=0.0):
    return EntityConfig(
        entity_id=ENTITY,
        name="Grid Status",
        default=default,
        states={},
        min_duration=min_duration,
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override the root conftest fixture; see tests/test_compiler.py."""
    yield


@pytest.fixture
async def recorder(recorder_mock, hass):
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


async def play(hass, freezer, timeline, entity_id=ENTITY):
    """Set each (datetime, state) in turn and let the recorder commit it."""
    for when, state in timeline:
        freezer.move_to(when)
        hass.states.async_set(entity_id, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()


async def test_tail_is_the_timeline_from_the_watermark(recorder, freezer):
    hass = recorder
    await play(
        hass,
        freezer,
        [
            (T0 - timedelta(hours=1), "off"),
            (T0 + timedelta(minutes=10), "on"),
            (T0 + timedelta(minutes=30), "off"),
        ],
    )
    freezer.move_to(T0 + timedelta(minutes=45))
    tail = await Compiler(hass).async_tail(
        cfg(), T0.timestamp(), (T0 + timedelta(minutes=45)).timestamp()
    )
    assert tail == Timeline(
        T0.timestamp(),
        "off",
        [
            ((T0 + timedelta(minutes=10)).timestamp(), "on"),
            ((T0 + timedelta(minutes=30)).timestamp(), "off"),
        ],
    )


async def test_tail_agrees_with_the_next_compile(recorder, freezer):
    """What the tail says an hour will hold is what the compile then writes."""
    hass = recorder
    await play(
        hass,
        freezer,
        [
            (T0 - timedelta(hours=1), "off"),
            (T0 + timedelta(minutes=15), "on"),
            (T0 + timedelta(minutes=45), "off"),
        ],
    )
    freezer.move_to(T0 + timedelta(minutes=50))
    compiler = Compiler(hass)
    tail = await compiler.async_tail(cfg(), T0.timestamp(), T0.timestamp() + HOUR)
    assert tail is not None
    freezer.move_to(T0 + timedelta(hours=1, minutes=5))
    await compiler.async_compile(cfg(), T0.timestamp())
    assert await read_sums(hass, ON_DURATION, T0, T0 + timedelta(hours=1)) == [
        pytest.approx(0.5)
    ]
    # Same rows, same carry, same transitions: half an hour on.
    assert tally(tail.carried, tail.transitions, T0.timestamp(), T0.timestamp() + HOUR)[
        "on"
    ] == (pytest.approx(1800.0), 1)


async def test_tail_writes_nothing(recorder, freezer):
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=1), "off"), (T0, "on")])
    freezer.move_to(T0 + timedelta(minutes=30))
    await Compiler(hass).async_tail(
        cfg(), T0.timestamp(), (T0 + timedelta(minutes=30)).timestamp()
    )
    assert await existing(hass) == []


async def test_tail_of_an_empty_window_is_none(recorder, freezer):
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=1), "off")])
    assert (
        await Compiler(hass).async_tail(cfg(), T0.timestamp(), T0.timestamp()) is None
    )


async def test_an_open_short_spell_is_provisional(recorder, freezer):
    """`ignore_short` drops a spell still open at `now` until it has lasted."""
    hass = recorder
    config = cfg(default="ignore_short", min_duration=600)
    await play(
        hass,
        freezer,
        [(T0 - timedelta(hours=1), "off"), (T0 + timedelta(minutes=5), "on")],
    )
    freezer.move_to(T0 + timedelta(minutes=10))
    tail = await Compiler(hass).async_tail(
        config, T0.timestamp(), (T0 + timedelta(minutes=10)).timestamp()
    )
    assert tail == Timeline(T0.timestamp(), "off", [])
    freezer.move_to(T0 + timedelta(minutes=20))
    tail = await Compiler(hass).async_tail(
        config, T0.timestamp(), (T0 + timedelta(minutes=20)).timestamp()
    )
    assert tail == Timeline(
        T0.timestamp(), "off", [((T0 + timedelta(minutes=5)).timestamp(), "on")]
    )


async def test_compile_signals_what_it_wrote(recorder, freezer):
    hass = recorder
    heard = []
    async_dispatcher_connect(
        hass, compiled_signal(ENTITY), lambda start, end: heard.append((start, end))
    )
    compiler = Compiler(hass)
    # Nothing to compile: no signal.
    await compiler.async_compile(cfg(), T0.timestamp())
    await hass.async_block_till_done()
    assert heard == []
    await play(hass, freezer, [(T0, "on"), (T0 + timedelta(hours=1), "off")])
    freezer.move_to(T0 + timedelta(hours=2))
    await compiler.async_compile(cfg(), T0.timestamp())
    await hass.async_block_till_done()
    # Up to the hour in progress, not into it.
    assert heard == [(T0.timestamp(), T0.timestamp() + 2 * HOUR)]


async def test_async_compiled_reports_the_watermark(recorder, freezer):
    hass = recorder
    compiler = Compiler(hass)
    assert await compiler.async_compiled(ENTITY) == ({}, None)
    await play(hass, freezer, [(T0, "on"), (T0 + timedelta(hours=1), "off")])
    freezer.move_to(T0 + timedelta(hours=2))
    await compiler.async_compile(cfg(), T0.timestamp())
    existing, watermark = await compiler.async_compiled(ENTITY)
    assert ON_DURATION in existing
    assert watermark == (T0 + timedelta(hours=1)).timestamp()


# --- history_stats' own scenarios ------------------------------------------
#
# Every scenario seeds a state before the window opens, since the tail
# needs a carried state to open with - the recorder's row before the
# window, or the state machine's proof - where history_stats fakes the
# start-time row.


def port_cfg(entity_id=ENTITY, states=None, min_duration=0.0):
    """`record` everything: history_stats has no notion of an ignored state."""
    return EntityConfig(
        entity_id=entity_id,
        name="Grid Status",
        default="record",
        states=states or {},
        min_duration=min_duration,
    )


async def measure(hass, cfg, start, end, states):
    """(hours, count, ratio) over [start, end) for `states`, as history_stats rounds them.

    The tail runs to now at the latest. The ratio divides by the window
    asked for, as theirs does; the share sensor divides by the part of the
    period that has elapsed, and the two agree whenever the window ends
    at now.
    """
    now = dt_util.utcnow()
    until = min(end, now).timestamp()
    tail = await Compiler(hass).async_tail(cfg, start.timestamp(), until)
    totals = (
        {}
        if tail is None
        else tally(
            tail.carried, tail.transitions, max(start.timestamp(), tail.start), until
        )
    )
    seconds = sum(totals.get(state, (0.0, 0))[0] for state in states)
    count = sum(totals.get(state, (0.0, 0))[1] for state in states)
    ratio = 100 * seconds / (end - start).total_seconds()
    return round(seconds / HOUR, 2), count, round(ratio, 1)


# Start     t0        t1        t2        End
# |--20min--|--10min--|--10min--|--20min--|
# |---off---|---on----|---off---|---on----|
def off_on_off_on(start):
    return [
        (start - timedelta(hours=2), "off"),
        (start + timedelta(minutes=20), "on"),
        (start + timedelta(minutes=30), "off"),
        (start + timedelta(minutes=40), "on"),
    ]


async def test_measure(recorder, freezer):
    hass = recorder
    await play(hass, freezer, off_on_off_on(T0))
    freezer.move_to(T0 + timedelta(minutes=60))
    assert await measure(hass, port_cfg(), T0, T0 + timedelta(hours=1), ["on"]) == (
        0.5,
        2,
        50.0,
    )


async def test_measure_multiple(recorder, freezer):
    """A set of states is summed; a blank state records as `unknown`."""
    hass = recorder
    entity_id = "input_select.test_id"
    await play(
        hass,
        freezer,
        [
            (T0 - timedelta(hours=2), ""),
            (T0 + timedelta(minutes=20), "orange"),
            (T0 + timedelta(minutes=30), "default"),
            (T0 + timedelta(minutes=40), "blue"),
        ],
        entity_id=entity_id,
    )
    freezer.move_to(T0 + timedelta(minutes=60))
    assert await measure(
        hass, port_cfg(entity_id), T0, T0 + timedelta(hours=1), ["orange", "blue"]
    ) == (0.5, 2, 50.0)


async def test_on_entire_period_counts_no_transition(recorder, freezer):
    """history_stats reports a count of 1 here: the block overlaps the window.

    Ours is the number of transitions inside the window, and there is
    none - the spell began before it. The README says so under the count
    metric.
    """
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=2), "on")])
    freezer.move_to(T0 + timedelta(minutes=60))
    assert await measure(hass, port_cfg(), T0, T0 + timedelta(hours=1), ["on"]) == (
        1.0,
        0,
        100.0,
    )


async def test_off_entire_period(recorder, freezer):
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=2), "off")])
    freezer.move_to(T0 + timedelta(minutes=60))
    assert await measure(hass, port_cfg(), T0, T0 + timedelta(hours=1), ["on"]) == (
        0.0,
        0,
        0.0,
    )


async def test_measure_from_end_going_backwards(recorder, freezer):
    """A window of one hour ending now, read halfway through the timeline.

    The rows after `now` are already in the recorder and must not count.
    """
    hass = recorder
    await play(hass, freezer, off_on_off_on(T0))
    now = T0 + timedelta(minutes=30)
    freezer.move_to(now)
    assert await measure(hass, port_cfg(), now - timedelta(hours=1), now, ["on"]) == (
        0.17,
        1,
        16.7,
    )


async def test_measure_cet(recorder, freezer):
    """The tail is timestamp arithmetic; the zone changes nothing."""
    hass = recorder
    await hass.config.async_set_time_zone("Europe/Berlin")
    await play(hass, freezer, off_on_off_on(T0))
    freezer.move_to(T0 + timedelta(minutes=60))
    assert await measure(hass, port_cfg(), T0, T0 + timedelta(hours=1), ["on"]) == (
        0.5,
        2,
        50.0,
    )


async def test_state_change_during_window_rollover(recorder, freezer):
    """A window of today, read across midnight, with changes either side.

    history_stats loses the first minute of the new day - its re-query at
    midnight hands it an `off` row at 00:00, though the entity was on until
    00:01 - and reads 2.0 where the recorder holds 2.02.
    """
    hass = recorder
    day = T0  # midnight UTC
    await play(
        hass,
        freezer,
        [(day + timedelta(hours=11), "off"), (day + timedelta(hours=12), "on")],
    )

    freezer.move_to(day + timedelta(hours=23))
    assert await measure(hass, port_cfg(), day, day + timedelta(days=1), ["on"]) == (
        11.0,
        1,
        45.8,
    )
    freezer.move_to(day + timedelta(hours=23, minutes=59, microseconds=300))
    hours, _, _ = await measure(hass, port_cfg(), day, day + timedelta(days=1), ["on"])
    assert hours == 11.98

    next_day = day + timedelta(days=1)
    await play(
        hass,
        freezer,
        [
            (next_day + timedelta(minutes=1), "off"),
            (next_day + timedelta(minutes=10), "on"),
            (next_day + timedelta(hours=1, minutes=10), "off"),
            (next_day + timedelta(hours=2, minutes=10), "on"),
        ],
    )
    freezer.move_to(next_day + timedelta(hours=3, minutes=10))
    assert await measure(
        hass, port_cfg(), next_day, next_day + timedelta(days=1), ["on"]
    ) == (2.02, 2, 8.4)


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(9, (0.0, 0, 0.0)), (10, (0.17, 1, 16.7)), (11, (0.18, 1, 18.3))],
)
async def test_around_min_state_duration(recorder, freezer, minutes, expected):
    """A spell of the target state counts once it has lasted the minimum.

    Their `min_state_duration` on the matched state is `ignore_short` on
    that state alone here. The window is the first hour of the day, so the
    ratio is over the hour while the tail runs to now.
    """
    hass = recorder
    cfg = port_cfg(states={"on": "ignore_short"}, min_duration=600.0)
    await play(hass, freezer, [(T0 - timedelta(hours=2), "off"), (T0, "on")])
    freezer.move_to(T0 + timedelta(minutes=minutes))
    assert await measure(hass, cfg, T0, T0 + timedelta(hours=1), ["on"]) == expected


async def test_measure_multiple_with_min_state_duration_diverges(recorder, freezer):
    """Where a short spell of a target state falls, the two disagree.

    # Start     t0        t1        t2        End
    # |--10min--|--10min--|--10min--|--10min--|
    # |---blue--|--orange-|-default-|---blue--|

    With fifteen minutes required of orange and blue, history_stats reads
    0.33 h at t1 and 0.5 h at t2 (core's
    `test_measure_multiple_with_min_state_duration`). Here every spell of a
    target state is judged on its own length: blue's ten minutes and
    orange's ten are both too short and fall to the state before them, so
    nothing is counted until the last blue spell has lasted fifteen
    minutes. `default` is not a target and is recorded as it stands.
    """
    hass = recorder
    entity_id = "input_select.test_id"
    cfg = port_cfg(
        entity_id,
        states={"orange": "ignore_short", "blue": "ignore_short"},
        min_duration=900.0,
    )
    await play(
        hass,
        freezer,
        [
            (T0 - timedelta(hours=2), "default"),
            (T0, "blue"),
            (T0 + timedelta(minutes=10), "orange"),
            (T0 + timedelta(minutes=20), "default"),
            (T0 + timedelta(minutes=30), "blue"),
        ],
        entity_id=entity_id,
    )
    window = (T0, T0 + timedelta(hours=1))
    for minutes in (10, 20, 30):
        freezer.move_to(T0 + timedelta(minutes=minutes))
        assert await measure(hass, cfg, *window, ["orange", "blue"]) == (0.0, 0, 0.0)
    freezer.move_to(T0 + timedelta(minutes=45))
    assert await measure(hass, cfg, *window, ["orange", "blue"]) == (0.25, 1, 25.0)
