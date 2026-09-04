"""Tests for the compiler against a real recorder."""

import functools as ft
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.setup import async_setup_component

from homeassistant.components.recorder.statistics import async_add_external_statistics

from custom_components.discrete_statistics import compiler as compiler_module
from custom_components.discrete_statistics.compiler import TRAILING_HOURS, Compiler
from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import HOUR, METRIC_COUNT, METRIC_DURATION
from custom_components.discrete_statistics.payload import metadata_for
from custom_components.discrete_statistics.statistic_ids import belongs_to, parse

ENTITY = "binary_sensor.grid_status"
DURATION_OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"
COUNT_OFF = "discrete_statistics:binary_sensor_grid_status_off_count"
DURATION_ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
COUNT_ON = "discrete_statistics:binary_sensor_grid_status_on_count"
DURATION_UNKNOWN = "discrete_statistics:binary_sensor_grid_status_unknown_duration"
DURATION_MISSING = "discrete_statistics:binary_sensor_grid_status_missing_duration"
COUNT_MISSING = "discrete_statistics:binary_sensor_grid_status_missing_count"


def cfg():
    return EntityConfig(
        entity_id=ENTITY, name="Grid Status", default="record_known", states={}
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override the root conftest fixture.

    The root fixture pulls in `hass`, which the recorder fixtures refuse to
    run behind: `recorder_db_url` asserts that hass has not been created
    yet. Requesting it first restores the required order.
    """
    yield


@pytest.fixture
async def recorder(recorder_mock, hass):
    """A hass with the recorder set up."""
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


async def existing(hass, entity_id=ENTITY):
    """The statistic IDs the recorder holds for an entity."""
    await get_instance(hass).async_block_till_done()
    metadata = await get_instance(hass).async_add_executor_job(
        ft.partial(get_metadata, hass, statistic_source="discrete_statistics")
    )
    return sorted(sid for sid in metadata if belongs_to(sid, entity_id))


async def stored_name(hass, statistic_id):
    await get_instance(hass).async_block_till_done()
    metadata = await get_instance(hass).async_add_executor_job(
        ft.partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    return metadata[statistic_id][1]["name"]


async def read_sums(hass, statistic_id, start, end):
    """Return the cumulative sums recorded for a statistic."""
    # async_add_external_statistics only queues the write, so drain the
    # recorder before querying it back.
    await get_instance(hass).async_block_till_done()
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return [row["sum"] for row in result.get(statistic_id, [])]


async def test_compiles_nothing_without_history(recorder):
    hass = recorder
    compiler = Compiler(hass)
    assert await compiler.async_compile_incremental(cfg()) == 0


async def test_registers_statistic_ids_it_writes(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    assert DURATION_ON in await existing(hass)
    assert await stored_name(hass, DURATION_ON) == "Grid Status: on (h)"


async def test_records_an_outage_duration_and_count(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(minutes=45))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    sums = await read_sums(
        hass, DURATION_OFF, start, start + timedelta(hours=2)
    )
    assert sums[0] == pytest.approx(0.25)
    counts = await read_sums(
        hass, COUNT_OFF, start, start + timedelta(hours=2)
    )
    assert counts[0] == 1


async def test_compiling_twice_is_idempotent(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    compiler = Compiler(hass)

    await compiler.async_compile(cfg(), start.timestamp())
    first = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=2))
    await compiler.async_compile(cfg(), start.timestamp())
    second = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=2))

    # "off" runs from 00:30 to the end of the window: 1800 s in the first
    # hour, a full hour in the second. Assert the values, not just that two
    # reads agree - they would agree if both were empty.
    assert first == [0.5, 1.5]
    assert first == second


async def test_cadence_invariance(recorder, freezer):
    """Compiling in advancing steps must equal compiling all at once."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    for offset, state in ((70, "off"), (100, "on"), (200, "off")):
        freezer.move_to(start + timedelta(minutes=offset))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    compiler = Compiler(hass)

    freezer.move_to(start + timedelta(hours=5))
    await compiler.async_compile(cfg(), start.timestamp())
    all_at_once = await read_sums(
        hass, DURATION_OFF, start, start + timedelta(hours=5)
    )
    assert all_at_once  # the comparison below is worthless if this is empty

    # Recompute the same range as a sliding window, the way incremental
    # compilation actually runs: each step ends an hour later than the last
    # and starts a trailing window back from its own end. Only the final
    # step's hours are rewritten by that step, so the earlier hours in the
    # result below are the ones the earlier steps wrote, and every step but
    # the first reads a non-empty base_sums.
    for hour in range(1, 6):
        step_end = start + timedelta(hours=hour)
        step_start = max(
            start.timestamp(), step_end.timestamp() - TRAILING_HOURS * HOUR
        )
        await compiler.async_compile(cfg(), step_start, step_end.timestamp())
    stepwise = await read_sums(
        hass, DURATION_OFF, start, start + timedelta(hours=5)
    )

    assert all_at_once == stepwise


async def test_the_trailing_window_picks_up_a_late_committed_state(
    recorder, freezer
):
    """The hourly run recompiles TRAILING_HOURS back from the watermark.

    A state change committed after its hour was first compiled sits in the
    oldest hour the trailing window reaches. Compiling only the newest hour
    or two would leave the early compile's attribution in place forever.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    compiler = Compiler(hass)
    await compiler.async_compile_incremental(cfg())
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=3))
    assert on == [1.0, 2.0, 3.0]

    # The watermark is hour 2, so the trailing window opens at hour 0 - the
    # hour this change lands in.
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await compiler.async_compile_incremental(cfg())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=4))
    assert on == [0.5, 0.5, 0.5, 0.5]
    assert off == [0.5, 1.5, 2.5, 3.5]


async def test_a_state_absent_from_a_window_keeps_its_cumulative_base(
    recorder, freezer
):
    """A statistic missing from a window must not restart its sum at zero.

    The windows are compiled back to back, deliberately without draining the
    recorder between them: the compiler drains its own writes before
    returning, so a caller compiling one window after another does not have
    to know about the queue.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    # "off" occurs in the first window and the third, but not the second.
    for offset, state in (
        (timedelta(minutes=30), "off"),
        (timedelta(hours=1), "on"),
        (timedelta(hours=4, minutes=15), "off"),
        (timedelta(hours=4, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    compiler = Compiler(hass)

    for window in range(3):
        window_start = start + timedelta(hours=2 * window)
        await compiler.async_compile(
            cfg(),
            window_start.timestamp(),
            (window_start + timedelta(hours=2)).timestamp(),
        )

    sums = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=6))

    # 1800 s in the first window, nothing in the second, 900 s in the third.
    assert sums == [0.5, 0.5, 0.5, 0.5, 0.75, 0.75]


async def test_watermark_is_the_newest_hour_across_statistics(recorder):
    """The watermark must not follow the alphabetically first statistic."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

    # COUNT_OFF sorts before DURATION_ON, and is the one left behind.
    for statistic_id, state, metric, hours in (
        (COUNT_OFF, "off", METRIC_COUNT, 2),
        (DURATION_ON, "on", METRIC_DURATION, 4),
    ):
        async_add_external_statistics(
            hass,
            metadata_for(metric, statistic_id, f"x: {state} ({metric})"),
            [
                {"start": start + timedelta(hours=hour), "sum": float(hour)}
                for hour in range(hours)
            ],
        )
    await get_instance(hass).async_block_till_done()

    compiler = Compiler(hass)
    ids = await existing(hass)
    assert ids[0] == COUNT_OFF
    watermark = await compiler._async_watermark({sid: "" for sid in ids})

    assert watermark == (start + timedelta(hours=3)).timestamp()


async def test_recompiling_back_before_the_first_state_writes_no_gap(
    recorder, freezer
):
    """A recompute asked to start before the recorder's evidence opens at
    the evidence instead.

    The hours before it have no rows to rebuild them from, so nothing is
    written there.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    await compiler.async_compile(cfg(), start.timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    assert on == [1.0, 2.0]


async def test_recomputing_past_the_purge_horizon_leaves_older_hours_alone(
    recorder, freezer
):
    """The case the clamp exists for.

    Months of statistics, ten days of recorder. A recompute from before the
    horizon must not rebuild the range from nothing, flattening every real
    sum to its base - a deletion by another name. Hours the recorder cannot
    vouch for are left exactly as they were compiled when it could.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for minutes, state in ((0, "on"), (90, "off"), (150, "on"), (390, "off")):
        freezer.move_to(start + timedelta(minutes=minutes))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=10))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    before = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=10))
    assert before == [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 5.5, 5.5, 5.5, 5.5]

    # Everything the recorder held is purged; one new row arrives after.
    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()
    freezer.move_to(start + timedelta(hours=10, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    # The hourly run keeps the watermark current, as it would in practice.
    freezer.move_to(start + timedelta(hours=11))
    await compiler.async_compile_incremental(cfg())

    # A recompute from before the horizon, over hours it cannot rebuild.
    freezer.move_to(start + timedelta(hours=12))
    await compiler.async_compile(cfg(), start.timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=12))
    assert on[:10] == before
    assert on[10:] == [6.0, 7.0]
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=12))
    assert off == [0.0, 0.5, 1.0, 1.0, 1.0, 1.0, 1.5, 2.5, 3.5, 4.5, 5.0, 5.0]


async def test_a_hole_after_the_watermark_is_still_filled(recorder, freezer):
    """The clamp stops at the hour after the watermark.

    Downtime longer than the purge horizon leaves hours that were never
    compiled between the watermark and the recorder's evidence. Opening at
    the hour after the watermark puts the watermark hour where the carry
    chain reads it, and here it was uniform - so our own last row vouches
    for the state, and the hole is filled with it rather than left open.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()
    freezer.move_to(start + timedelta(hours=5, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=7))
    await compiler.async_compile(cfg(), start.timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=7))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=7))
    # Hours 0-1 untouched; hours 2-4 are "off", the state hour 1 vouches
    # for; hour 5 splits at the return; hour 6 is "on".
    assert on == [0.5, 0.5, 0.5, 0.5, 0.5, 1.0, 2.0]
    assert off == [0.5, 1.5, 2.5, 3.5, 4.5, 5.0, 5.0]


async def test_a_hole_nothing_can_vouch_for_is_left_open(recorder, freezer):
    """Downtime past the horizon, and the watermark hour was not uniform.

    No source can say what state the entity held when the hole opened, so
    the hours are not compiled at all. The series resumes at the first
    whole hour the recorder can vouch for, and the sums continue from the
    last row before the hole: time we cannot describe is time in no state.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    # Hour 0 alone is compiled, so the watermark hour holds a transition.
    freezer.move_to(start + timedelta(hours=1))
    compiler = Compiler(hass)
    await compiler.async_compile_incremental(cfg())

    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()
    freezer.move_to(start + timedelta(hours=5, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=7))
    assert await compiler.async_compile_incremental(cfg()) == 1

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=7))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=7))
    # Hour 0, then nothing until hour 6 - which continues from hour 0.
    assert on == [0.5, 1.5]
    assert off == [0.5, 0.5]

    # The next hourly run opens on the far side of the hole, where the hour
    # before its window has no row at all, and still finds its base.
    freezer.move_to(start + timedelta(hours=8))
    await compiler.async_compile_incremental(cfg())
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=8))
    assert on == [0.5, 1.5, 2.5]


async def test_a_recompute_opening_inside_a_hole_bases_on_the_row_before_it(
    recorder, freezer
):
    """Rows on both sides of the hole: the newest is not the base.

    The base must be the newest row *before* the window, and after a hole
    that is not the hour before it. A recompute asked to start inside the
    hole opens on its far side and must continue from the near side, not
    from the rows it is about to overwrite.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=1))
    compiler = Compiler(hass)
    await compiler.async_compile_incremental(cfg())
    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()
    freezer.move_to(start + timedelta(hours=5, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    freezer.move_to(start + timedelta(hours=8))
    await compiler.async_compile_incremental(cfg())
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=8))
    assert on == [0.5, 1.5, 2.5]

    await compiler.async_compile(cfg(), (start + timedelta(hours=3)).timestamp())
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=8))
    assert on == [0.5, 1.5, 2.5]


async def test_hours_that_cannot_be_recomputed_are_left_as_they_are(
    recorder, freezer
):
    """A recompute reaches an hour no source can open, with rows behind it.

    The purge horizon fell inside the lookback hour and spared only an
    ignored row, the hour before was not uniform, and the live state began
    later. Those hours were compiled correctly when the recorder could
    still vouch for them; the recompute passes over them and resumes at
    the first hour it can open, basing on the rows it kept.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for minutes, state in ((0, "off"), (20, "on"), (40, "unavailable"), (70, "off")):
        freezer.move_to(start + timedelta(minutes=minutes))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=2))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=2))
    assert on == pytest.approx([2 / 3, 5 / 6])
    assert off == pytest.approx([1 / 3, 7 / 6])

    # The horizon lands at 0:30: the recordable rows go, the ignored one stays.
    freezer.move_to(start + timedelta(minutes=30))
    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    await compiler.async_compile(cfg(), start.timestamp())
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=3))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=3))
    assert on == pytest.approx([2 / 3, 5 / 6, 5 / 6])
    assert off == pytest.approx([1 / 3, 7 / 6, 13 / 6])


async def test_an_ignored_state_at_the_window_start_does_not_destroy_durations(
    recorder, freezer
):
    """A trailing compile across an ignored stretch must agree with a full one.

    `include_start_time_state` returns exactly one row before the boundary.
    When that row is `unavailable`, the recordable state one row further back
    is invisible and the window would open in no known state at all - and
    the correct rows would be lost, because the upsert is idempotent and the
    window start moves every run.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for offset, state in (
        (timedelta(0), "on"),
        (timedelta(minutes=30), "unavailable"),
        (timedelta(hours=2, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)

    await compiler.async_compile(cfg(), start.timestamp())
    full = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    # `unavailable` carries "on" forward, so the entity is "on" throughout.
    assert full == [1.0, 2.0, 3.0, 4.0]

    # Now the trailing window the hourly run would use, whose start lands in
    # the middle of the ignored stretch.
    await compiler.async_compile(
        cfg(), (start + timedelta(hours=1)).timestamp()
    )
    trailing = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))

    assert trailing == full


async def test_a_boundary_transition_is_counted_once_from_either_window(
    recorder, freezer
):
    """A transition exactly on an hour must not depend on the window start."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for offset, state in (
        (timedelta(0), "on"),
        (timedelta(hours=2), "off"),
        (timedelta(hours=2, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)

    await compiler.async_compile(cfg(), start.timestamp())
    from_earlier = await read_sums(
        hass, COUNT_OFF, start, start + timedelta(hours=4)
    )
    assert from_earlier == [0, 0, 1, 1]

    # Recompile a window that begins exactly on the transition.
    await compiler.async_compile(
        cfg(), (start + timedelta(hours=2)).timestamp()
    )
    from_boundary = await read_sums(
        hass, COUNT_OFF, start, start + timedelta(hours=4)
    )

    assert from_boundary == from_earlier

    # Durations are untouched by the boundary event.
    seconds = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=4))
    assert seconds == [0.0, 0.0, 0.5, 0.5]


async def test_compiling_across_a_chunk_boundary(recorder, freezer, monkeypatch):
    """A window longer than CHUNK_HOURS must carry sums across the seam."""
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 2)

    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    # Transitions on both sides of every 2-hour chunk seam.
    for offset, state in (
        (timedelta(minutes=30), "off"),
        (timedelta(hours=1, minutes=45), "on"),
        (timedelta(hours=2, minutes=15), "off"),
        (timedelta(hours=3, minutes=45), "on"),
        (timedelta(hours=4, minutes=30), "off"),
        (timedelta(hours=5, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    compiler = Compiler(hass)
    assert await compiler.async_compile(cfg(), start.timestamp()) == 6

    end = start + timedelta(hours=6)
    duration_ids = [
        statistic_id
        for statistic_id in await existing(hass)
        if parse(statistic_id)[2] == METRIC_DURATION
    ]
    assert len(duration_ids) >= 2, duration_ids

    total = 0.0
    for statistic_id in duration_ids:
        sums = await read_sums(hass, statistic_id, start, end)
        assert len(sums) == 6, (statistic_id, sums)
        assert sums == sorted(sums), f"{statistic_id} went backwards: {sums}"
        total += sums[-1]

    # Time is conserved across the seam: every second of the six hours is
    # attributed to exactly one state.
    assert total == pytest.approx(6.0)

    off_duration = await read_sums(hass, DURATION_OFF, start, end)
    # off runs 0:30-1:45, 2:15-3:45 and 4:30-5:30.
    assert off_duration[-1] == pytest.approx((4500 + 5400 + 3600) / 3600)


async def read_day(hass, statistic_id, start, end):
    """Return the day-period rollup rows for a statistic."""
    await get_instance(hass).async_block_till_done()
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "day",
        None,
        {"mean", "min", "max", "sum"},
    )
    return result.get(statistic_id, [])


async def test_hourly_values_roll_up_into_a_daily_mean_min_and_max(recorder, freezer):
    """The point of writing mean/min/max: second-order statistics for free.

    The recorder reduces the hourly rows itself, so a statistics-graph card
    asking for `mean` over a day answers "average hours on per hour" and
    `max` answers "the busiest hour" - neither of which the cumulative sum
    can express.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(hours=3, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    # Hourly "on" durations are 1.0, 1.0, 0.0, 0.5.
    hourly = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    assert hourly == [1.0, 2.0, 2.0, 2.5]

    rows = await read_day(
        hass, DURATION_ON, start - timedelta(days=1), start + timedelta(days=2)
    )
    assert len(rows) == 1
    assert rows[0]["mean"] == pytest.approx(2.5 / 4)
    assert rows[0]["min"] == 0.0
    assert rows[0]["max"] == 1.0
    # The sum is untouched by the reduction: it stays the cumulative total.
    assert rows[0]["sum"] == pytest.approx(2.5)


async def test_a_new_entity_opens_at_the_first_whole_hour_it_knows(
    recorder, freezer
):
    """An entity's first state almost never lands on the hour, and the
    part-known hour containing it cannot both be recorded and total
    wall-clock time."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2, minutes=23))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=5))
    compiler = Compiler(hass)
    hours = await compiler.async_compile(cfg(), start.timestamp())

    # Hours 3 and 4 only: the partial hour 2 is dropped along with hours 0-1.
    assert hours == 2
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=5)) == [
        1.0,
        2.0,
    ]


async def test_a_first_state_on_the_hour_loses_nothing(recorder, freezer):
    """No hour is trimmed when the first state already sits on a boundary.

    Nor is that state counted as a transition. Mid-hour the trim makes it
    the carried state; on the hour the trim is a no-op and canonicalise
    leaves it as a transition - but nothing transitioned INTO an entity's
    first known state, so neither is counted.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=5))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=5)) == [
        1.0,
        2.0,
        3.0,
    ]
    assert await read_sums(hass, COUNT_ON, start, start + timedelta(hours=5)) == [
        0,
        0,
        0,
    ]


async def test_an_unknown_opening_state_is_trimmed_too(recorder, freezer):
    """`unknown` is the usual first state, and it is not a state we record.

    Trimming to the hour containing the first *row* would leave the gap in
    place; it has to be the first row the config actually resolves.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=10))
    hass.states.async_set(ENTITY, "unknown")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(hours=2, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=5))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=5)) == [
        1.0,
        2.0,
    ]


async def test_the_incremental_first_run_is_trimmed(recorder, freezer):
    """The watermark-less path is how an entry actually gets its first run."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=17))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=1, minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    compiler = Compiler(hass)
    await compiler.async_compile_incremental(cfg())

    # Hours 1 and 2, and they still tile the clock exactly.
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=3))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=3))
    assert on == [0.5, 0.5]
    assert off == [0.5, 1.5]


async def test_nothing_is_compiled_until_a_whole_hour_is_known(recorder, freezer):
    """An entry created mid-hour waits for the next one rather than inventing.

    Hour 0 has completed, so there is an hour to compile - but it is only
    known from 00:20, and the first hour known end to end has not finished
    yet. A part-known hour cannot both be recorded and total wall-clock
    time.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=20))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=1, minutes=30))
    compiler = Compiler(hass)

    assert await compiler.async_compile_incremental(cfg()) == 0
    assert await existing(hass) == []


async def _seed_two_states(hass, freezer, start):
    """on for hour 0, off from hour 1 onward. No trim: the first state is on the hour."""
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=1))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()


async def _delete(hass, statistic_ids):
    get_instance(hass).async_clear_statistics(list(statistic_ids))
    await get_instance(hass).async_block_till_done()


async def test_a_deleted_statistic_is_forgotten_not_recreated(recorder, freezer):
    """Settings -> System -> Tools -> Statistics is the whole interface.

    Deleting removes the statistics_meta row, so the statistic is simply
    absent from the entity's set on the next compile and stops being
    written.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    assert DURATION_ON in await existing(hass)

    # "on" never occurs again, so only a stale index could resurrect it.
    await _delete(hass, [DURATION_ON, COUNT_ON])

    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile_incremental(cfg())

    assert DURATION_ON not in await existing(hass)
    assert COUNT_ON not in await existing(hass)
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=6)) == []
    # The surviving state carries on undisturbed, still dense and monotonic.
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=6))
    assert off == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


async def test_deleting_one_metric_sticks_until_its_state_recurs(recorder, freezer):
    """Deletion is per statistic, not per state.

    Density keyed by state would undo it on the very next compile, the
    surviving count keeping "on" alive. Keyed by statistic it sticks - until
    "on" actually happens again, at which point an observed state is
    recorded in full.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    await _delete(hass, [DURATION_ON])

    # "on" does not occur in the trailing window, so it stays gone.
    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile_incremental(cfg())
    assert DURATION_ON not in await existing(hass)
    assert COUNT_ON in await existing(hass)

    # It happens again, and both of its metrics come back.
    freezer.move_to(start + timedelta(hours=6, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=8))
    await compiler.async_compile_incremental(cfg())
    assert DURATION_ON in await existing(hass)


async def test_a_healthy_entity_keeps_every_statistic_dense(recorder, freezer):
    """The density invariant, asserted on rows rather than on ID membership.

    Membership alone proves nothing: nothing in this integration ever removes
    a statistics_meta row, so `existing == before` holds however badly the
    compile behaves. What must hold is that a state absent from the window
    still gets a row in each of its hours, carrying its sum forward.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile_incremental(cfg())

    # "on" happened only in hour 0 and never again, so every later hour is a
    # carried row: same sum, and a zero hourly value.
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=6))
    assert on == [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=6))
    assert off == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    # And the two still tile the clock in every hour.
    for hour, (a, b) in enumerate(zip(on, off)):
        total = (a - (on[hour - 1] if hour else 0.0)) + (
            b - (off[hour - 1] if hour else 0.0)
        )
        assert total == pytest.approx(1.0), (hour, a, b)


async def test_a_statistic_created_in_one_chunk_stays_dense_in_the_next(
    recorder, freezer, monkeypatch
):
    """The carry-forward of newly created statistics across a chunk seam.

    A statistic first written in chunk N is NOT yet in the recorder's
    metadata when chunk N+1 asks - the write is still queued - so the chunk
    has to hand it forward itself. Without that, "on" would have no row in
    hours 2 onward, and the next window would find no base in the hour before
    it and restart the series at zero.
    """
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 2)

    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    # "on" ends inside the first chunk and never returns.
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    compiler = Compiler(hass)
    assert await compiler.async_compile(cfg(), start.timestamp()) == 6

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=6))
    assert on == [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]


async def test_an_entity_with_no_recordable_state_compiles_nothing(recorder, freezer):
    """Every row resolves to nothing, so there is no first hour to start at.

    Without the guard the trim indexes transitions[0] on an empty list and
    the hourly run for this entity dies with an IndexError.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for offset in (timedelta(minutes=5), timedelta(hours=1), timedelta(hours=2)):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, "unavailable")
        await hass.async_block_till_done()
        hass.states.async_set(ENTITY, "unknown")
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)

    # record_known ignores both states, so nothing is recordable at all.
    assert await compiler.async_compile(cfg(), start.timestamp()) == 0
    assert await existing(hass) == []


OTHER = "binary_sensor.grid_status_pump"
OTHER_DURATION_ON = "discrete_statistics:binary_sensor_grid_status_pump_on_duration"


async def test_two_entities_never_write_into_each_others_statistics(
    recorder, freezer
):
    """`belongs_to` is what keeps them apart, and it is load-bearing.

    The entity IDs are chosen so one slug is a prefix of the other at an
    underscore boundary - the case a multi-token state would collide on.
    A filter that is too broad would have each compile write dense rows into
    the other entity's series, and rename its metadata to its own name.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    other_cfg = EntityConfig(
        entity_id=OTHER, name="Pump", default="record_known", states={}
    )

    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    hass.states.async_set(OTHER, "off")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(OTHER, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    await compiler.async_compile(other_cfg, start.timestamp())

    assert OTHER_DURATION_ON not in await existing(hass, ENTITY)
    assert DURATION_ON not in await existing(hass, OTHER)

    # Each keeps its own name, and its own values.
    assert await stored_name(hass, DURATION_ON) == "Grid Status: on (h)"
    assert await stored_name(hass, OTHER_DURATION_ON) == "Pump: on (h)"
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4)) == [
        1.0,
        2.0,
        3.0,
        4.0,
    ]
    assert await read_sums(
        hass, OTHER_DURATION_ON, start, start + timedelta(hours=4)
    ) == [0.0, 0.0, 1.0, 2.0]


async def test_a_boundary_row_into_the_carried_state_is_not_a_transition(
    recorder, freezer, monkeypatch
):
    """on, then an ignored row, then on again exactly on a chunk seam.

    The ignored row is carried forward, so the entity was on throughout
    and nothing transitioned. The second chunk sees a row into `on` at its
    boundary and is handed `on` as the carried state; without the dedupe it
    counted a change from on to on.
    """
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 2)
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for minutes, state in ((0, "on"), (90, "unavailable"), (120, "on")):
        freezer.move_to(start + timedelta(minutes=minutes))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await Compiler(hass).async_compile(cfg(), start.timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    assert on == [1.0, 2.0, 3.0, 4.0]
    assert await read_sums(hass, COUNT_ON, start, start + timedelta(hours=4)) == [
        0, 0, 0, 0
    ]


async def test_a_reloaded_entity_does_not_kill_the_compile(recorder, freezer):
    """A reload writes an empty state and restores the real one moments later.
    It cannot go in a statistic ID, and letting build() raise aborted the
    entity's whole compile - permanently, because the watermark never got
    past the chunk containing it. It is treated as `unknown` instead, so under
    record_known it is ignored and the previous state simply continues.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(hours=1))
    hass.states.async_set(ENTITY, "")  # reloaded
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    assert await compiler.async_compile(cfg(), start.timestamp()) == 4

    # record_known ignores it, so "on" carries straight through.
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    assert on == [1.0, 2.0, 3.0, 4.0]


async def test_a_reloaded_entity_is_recorded_as_unknown_under_record(
    recorder, freezer
):
    """Under `record` it lands in the unknown statistic."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    record_all = EntityConfig(
        entity_id=ENTITY, name="Grid Status", default="record", states={}
    )
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=1))
    hass.states.async_set(ENTITY, "")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=3))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await Compiler(hass).async_compile(record_all, start.timestamp())

    unknown = await read_sums(
        hass, DURATION_UNKNOWN, start, start + timedelta(hours=4)
    )
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    assert unknown == [0.0, 1.0, 2.0, 2.0]
    assert on == [1.0, 1.0, 1.0, 2.0]
    for hour in range(4):
        spent = (on[hour] - (on[hour - 1] if hour else 0.0)) + (
            unknown[hour] - (unknown[hour - 1] if hour else 0.0)
        )
        assert spent == pytest.approx(1.0), hour


async def test_a_blank_substitute_survives_the_whole_pipeline(recorder, freezer):
    """`blank:` names a state that reaches the recorder like any other."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    substituted = EntityConfig(
        entity_id=ENTITY,
        name="Grid Status",
        default="record",
        states={},
        blank="missing",
    )
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=1))
    hass.states.async_set(ENTITY, "")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=3))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await Compiler(hass).async_compile(substituted, start.timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    gap = await read_sums(hass, DURATION_MISSING, start, start + timedelta(hours=4))
    assert on == [1.0, 1.0, 1.0, 2.0]
    assert gap == [0.0, 1.0, 2.0, 2.0]
    for hour in range(4):
        spent = (on[hour] - (on[hour - 1] if hour else 0.0)) + (
            gap[hour] - (gap[hour - 1] if hour else 0.0)
        )
        assert spent == pytest.approx(1.0), hour
    # A state like any other: it is counted too.
    assert await read_sums(hass, COUNT_MISSING, start, start + timedelta(hours=4)) == [
        0.0,
        1.0,
        1.0,
        1.0,
    ]


UNNAMED = EntityConfig(
    entity_id=ENTITY, name=None, default="record_known", states={}
)


async def test_the_statistic_name_falls_back_to_the_entitys_name(recorder, freezer):
    """Not to its ID: the friendly name is what people see everywhere else."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on", {"friendly_name": "Mains Power"})
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(UNNAMED, start.timestamp())

    assert await stored_name(hass, DURATION_ON) == "Mains Power: on (h)"


async def test_a_typed_name_still_wins(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on", {"friendly_name": "Mains Power"})
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(cfg(), start.timestamp())

    assert await stored_name(hass, DURATION_ON) == "Grid Status: on (h)"


async def test_the_entity_id_is_the_last_resort(recorder, freezer):
    """No typed name, no registry entry, no friendly name."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(UNNAMED, start.timestamp())

    assert await stored_name(hass, DURATION_ON) == f"{ENTITY}: on (h)"


async def test_the_registry_name_survives_the_entity_being_unavailable(
    recorder, freezer, entity_registry
):
    """Attributes are stripped when unavailable; the registry entry is not.

    Reading the live state first would rename every statistic to the entity
    ID for as long as it was away, and rename them back afterwards.
    """
    hass = recorder
    entity_registry.async_get_or_create(
        "binary_sensor", "demo", "unique-1", suggested_object_id="grid_status",
        original_name="Mains Power",
    )
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=1))
    hass.states.async_set(ENTITY, "unavailable")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(UNNAMED, start.timestamp())

    assert await stored_name(hass, DURATION_ON) == "Mains Power: on (h)"


async def test_the_registry_name_wins_over_a_stale_friendly_name(
    recorder, freezer, entity_registry
):
    """They disagree only after a rename the integration has not republished.

    The registry holds what the user asked for, so it is the authority. This
    is what actually makes the lookup order matter - the unavailable case
    reaches the registry either way, by falling through.
    """
    hass = recorder
    entity_registry.async_get_or_create(
        "binary_sensor", "demo", "unique-2", suggested_object_id="grid_status",
        original_name="Old Name",
    )
    entity_registry.async_update_entity(ENTITY, name="Mains Power")

    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on", {"friendly_name": "Old Name"})
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(UNNAMED, start.timestamp())

    assert await stored_name(hass, DURATION_ON) == "Mains Power: on (h)"


async def test_states_are_rendered_the_way_home_assistant_renders_them(
    recorder, freezer, entity_registry
):
    """A door sensor reads Open/Closed in the UI, so the legend should too.

    Verifies the whole chain: registry device class, warmed translations,
    and the raw state where no translation exists.
    """
    hass = recorder
    assert await async_setup_component(hass, "binary_sensor", {})
    await hass.async_block_till_done()
    entity_registry.async_get_or_create(
        "binary_sensor", "demo", "door-1",
        suggested_object_id="grid_status",
        original_name="Front Door",
        original_device_class="door",
    )

    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=1))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(UNNAMED, start.timestamp())

    assert await stored_name(hass, DURATION_ON) == "Front Door: Open (h)"
    assert await stored_name(hass, DURATION_OFF) == "Front Door: Closed (h)"


async def test_an_untranslatable_state_keeps_its_raw_name(recorder, freezer):
    """Most enum sensors have no translations."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(cfg(), start.timestamp())

    assert await stored_name(hass, DURATION_ON) == "Grid Status: on (h)"


async def test_a_full_recompute_is_idempotent(recorder, freezer):
    """A bare `recompute` compiles from the beginning, and must be idempotent.

    The second full compile must skip the same opening sliver the first
    did, rather than writing the part-known hour before the first state.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2, minutes=23))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    compiler = Compiler(hass)
    await compiler.async_compile_incremental(cfg())

    await compiler.async_compile(cfg(), None)

    # And pressing it again changes nothing.
    await compiler.async_compile(cfg(), None)
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=6)) == [
        1.0,
        2.0,
        3.0,
    ]


async def test_a_full_recompute_still_trims_only_its_opening_chunk(
    recorder, freezer, monkeypatch
):
    """A later chunk is handed the state the one before ended in, so it
    never has to move its start."""
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 2)

    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=3))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), None)
    await compiler.async_compile(cfg(), None)

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=6))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=6))
    # Dense from hour 1, and every hour still totals wall-clock time.
    assert len(on) == len(off) == 5
    for hour in range(5):
        spent = (on[hour] - (on[hour - 1] if hour else 0.0)) + (
            off[hour] - (off[hour - 1] if hour else 0.0)
        )
        assert spent == pytest.approx(1.0), hour


async def test_the_carried_state_threads_across_a_chunk_seam(
    recorder, freezer, monkeypatch
):
    """A seam landing inside an ignored stretch keeps the state.

    `include_start_time_state` hands back the `unavailable` row at the
    boundary, which resolves to nothing, so the chunk has no state of its
    own. The previous chunk's ending state is what carries it, without a
    query or a distance limit.
    """
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 2)

    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "unavailable")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    assert await Compiler(hass).async_compile(cfg(), start.timestamp()) == 6

    # record_known ignores `unavailable`, so `on` runs through all six hours
    # and across both seams.
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=6))
    assert on == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


async def test_unknown_and_unavailable_are_capitalised(recorder, freezer):
    """Home Assistant renders these two from the frontend's own strings.

    `async_translate_state` returns them untouched, so without help a legend
    reads `unavailable` beside a properly rendered `Closed`.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    record_all = EntityConfig(
        entity_id=ENTITY, name="Grid Status", default="record", states={}
    )
    for offset, state in (
        (timedelta(0), "on"),
        (timedelta(hours=1), "unavailable"),
        (timedelta(hours=2), "unknown"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await Compiler(hass).async_compile(record_all, start.timestamp())

    assert await stored_name(
        hass, "discrete_statistics:binary_sensor_grid_status_unavailable_duration"
    ) == "Grid Status: Unavailable (h)"
    assert await stored_name(hass, DURATION_UNKNOWN) == "Grid Status: Unknown (h)"


async def test_a_state_older_than_the_purge_horizon_is_still_carried(
    recorder, freezer
):
    """The case that matters most once purge_keep_days is short.

    An entity that sits in one state for longer than the horizon has no rows
    left at all - purge deletes every row past it, with no per-entity
    reprieve - so nothing would be compiled for it. The state machine still
    knows, and knows since when: its whole span in one state, and no
    transitions into it.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    # Everything the recorder held about it is gone. Purge deletes rows
    # older than *now*, so the clock has to have moved past them first.
    freezer.move_to(start + timedelta(hours=4))
    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()
    # Nothing left in the recorder, so the opening moment now comes from the
    # live state: it began exactly on the hour, so that hour is usable whole.
    assert await Compiler(hass)._async_earliest_state_ts(ENTITY) == start.timestamp()

    await Compiler(hass).async_compile(
        cfg(), (start + timedelta(hours=1)).timestamp()
    )

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    counts = await read_sums(hass, COUNT_ON, start, start + timedelta(hours=4))
    assert on == [1.0, 2.0, 3.0]
    assert counts == [0, 0, 0]


async def test_a_state_that_began_inside_the_window_is_not_carried(
    recorder, freezer
):
    """It says nothing about how the window opened.

    This is also what stops a backfill of old hours being handed whatever
    the entity happens to be doing today.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    freezer.move_to(start + timedelta(hours=4))
    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()

    compiler = Compiler(hass)
    # last_changed is hour 2, the window opens at hour 0.
    assert compiler._carried_from_state_machine(cfg(), start.timestamp()) is None
    # And at hour 3 it is in effect, so it is carried.
    assert (
        compiler._carried_from_state_machine(
            cfg(), (start + timedelta(hours=3)).timestamp()
        )
        == "on"
    )


async def test_an_ignored_live_state_is_not_carried(recorder, freezer):
    """`unavailable` under record_known is still nothing to carry."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "unavailable")
    await hass.async_block_till_done()

    compiler = Compiler(hass)
    assert (
        compiler._carried_from_state_machine(
            cfg(), (start + timedelta(hours=2)).timestamp()
        )
        is None
    )


async def test_an_entity_with_neither_history_nor_a_state_compiles_nothing(
    recorder, freezer
):
    """There is nothing to say about it, so nothing is said."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=4))

    assert await Compiler(hass).async_compile_incremental(cfg()) == 0
    assert await existing(hass) == []


async def test_a_live_state_that_began_this_hour_waits(recorder, freezer):
    """A part-known hour cannot both be recorded and total wall-clock time."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=20))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(minutes=50))
    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()

    assert await Compiler(hass).async_compile_incremental(cfg()) == 0


async def test_the_opening_hour_is_whole_when_it_comes_from_the_live_state(
    recorder, freezer
):
    """Rounding up is what makes the carried state vouchable.

    `_carried_from_state_machine` requires `last_changed <= window_start`.
    Opening at the hour *containing* the change fails that test, so nothing
    carries, no transition exists to trim to, and the entity compiles zero
    hours instead of the hours it plainly held.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=20))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2, minutes=30))
    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()

    assert await Compiler(hass).async_compile_incremental(cfg()) == 1
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=3))
    assert on == [1.0]


async def test_an_ignored_row_at_the_boundary_does_not_hide_the_state_behind_it(
    recorder, freezer
):
    """`include_start_time_state` returns exactly one row before the window.

    When that row is `unavailable`, the recordable state moments earlier is
    invisible to it. Reading the previous hour whole is what surfaces both.
    Nothing else can help here: it is the entity's first compile, so there
    are no statistics, and its live state is the ignored one.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=1))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=1, minutes=40))
    hass.states.async_set(ENTITY, "unavailable")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=5))
    # The window opens at hour 2, past both rows: the only row before it is
    # the `unavailable`.
    await Compiler(hass).async_compile(
        cfg(), (start + timedelta(hours=2)).timestamp()
    )

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=5))
    assert on == [1.0, 2.0, 3.0]


async def test_a_long_ignored_stretch_is_carried_by_our_own_statistics(
    recorder, freezer
):
    """The case with no distance limit.

    The entity has been `unavailable` for hours, so the recorder has nothing
    recordable within the extra hour and its live state is the ignored one.
    Our own rows for the previous hour do know: a whole hour with nothing to
    record means it held one state throughout, and that is what they say.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "unavailable")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    # Hours 3 onward: the previous hour is entirely inside the ignored
    # stretch, so the recorder has nothing to offer even an hour back.
    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile(cfg(), (start + timedelta(hours=3)).timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=6))
    assert on == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


DURATION_HEATCOOL = "discrete_statistics:binary_sensor_grid_status_heatcool_duration"


async def test_a_state_carried_from_statistics_keeps_its_readable_name(
    recorder, freezer
):
    """A statistic ID holds only the token, its stored name holds the state.

    Carrying out of a statistic and composing from the token would rename
    `heat_cool` to `heatcool` for as long as nothing transitioned - written
    into the metadata, so visible on every chart.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "heat_cool")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "unavailable")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    assert await stored_name(hass, DURATION_HEATCOOL) == "Grid Status: heat_cool (h)"

    # Hours 3 on are carried out of the statistics, not out of any row.
    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile(cfg(), (start + timedelta(hours=3)).timestamp())

    assert await stored_name(hass, DURATION_HEATCOOL) == "Grid Status: heat_cool (h)"
    assert await read_sums(
        hass, DURATION_HEATCOOL, start, start + timedelta(hours=6)
    ) == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


def test_readable_state_is_verified_against_the_token():
    """Trusting the name blindly would invent a state and split the series.

    Whatever sits after the last ": " becomes the bucket key, and a wrong
    one builds a different statistic ID. So the recovered text has to
    tokenise back to the token the ID actually carries.
    """
    assert compiler_module._readable_state("Grid: heat_cool (h)", "heatcool") == (
        "heat_cool"
    )
    # A display name may hold colons of its own; the state is the last part.
    assert compiler_module._readable_state(
        "Shed: Grid: heat_cool (h)", "heatcool"
    ) == "heat_cool"
    # Renamed by hand, or written by an older format: no shape to read.
    assert compiler_module._readable_state("renamed by hand", "heatcool") == "heatcool"
    # Right shape, wrong state - the name does not belong to this ID.
    assert compiler_module._readable_state("Grid: off (h)", "heatcool") == "heatcool"
    assert compiler_module._readable_state("", "heatcool") == "heatcool"


async def test_a_chunk_that_raises_still_drains_the_ones_before_it(
    recorder, freezer, monkeypatch
):
    """The writes of the chunks before it are committed, not left queued.

    Density is read live from `statistics_meta`, so a compile that returned
    with rows still queued would let the next one see half of them.
    """
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 2)
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for minutes, state in ((0, "on"), (30, "off"), (150, "on")):
        freezer.move_to(start + timedelta(minutes=minutes))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    real = compiler._async_compile_chunk

    async def second_chunk_raises(cfg, chunk_start, *args, **kwargs):
        if chunk_start == (start + timedelta(hours=2)).timestamp():
            raise RuntimeError("chunk failed")
        return await real(cfg, chunk_start, *args, **kwargs)

    monkeypatch.setattr(compiler, "_async_compile_chunk", second_chunk_raises)
    with pytest.raises(RuntimeError, match="chunk failed"):
        await compiler.async_compile(cfg(), start.timestamp())

    # Read straight from the database, without draining the queue here.
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        start + timedelta(hours=4),
        {DURATION_ON, DURATION_OFF},
        "hour",
        None,
        {"sum"},
    )
    assert [row["sum"] for row in result[DURATION_ON]] == [0.5, 0.5]
    assert [row["sum"] for row in result[DURATION_OFF]] == [0.5, 1.5]


async def test_the_state_machine_outranks_our_own_statistics(recorder, freezer):
    """When the two disagree, the statistics are stale.

    `last_changed <= window_start` proves the live state was already in
    effect; a uniform hour in our rows only records what the recorder held
    when that hour was compiled. Here a change committed late in hour 1
    was purged before the hour could be recompiled, so the rows still say
    `off` while the entity has been `on` since before the window opened.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())

    # Committed after hour 1 was compiled, then purged with everything else.
    freezer.move_to(start + timedelta(hours=1, minutes=45))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    freezer.move_to(start + timedelta(hours=2))
    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await compiler.async_compile(cfg(), (start + timedelta(hours=2)).timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=4))
    assert on == [0.5, 0.5, 1.5, 2.5]
    assert off == [0.5, 1.5, 1.5, 1.5]


async def test_a_rename_reaches_an_absent_state_through_the_compiler(
    recorder, freezer
):
    """Through the compiler, not only `payload.rename`.

    The entity has not been `off` since hour 0, and the window being
    recompiled never sees that state. Its statistic is relabelled all the
    same, from the name the recorder already holds for it.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for minutes, state in ((0, "off"), (30, "on")):
        freezer.move_to(start + timedelta(minutes=minutes))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    assert await stored_name(hass, DURATION_OFF) == "Grid Status: off (h)"

    renamed = EntityConfig(
        entity_id=ENTITY, name="Mains", default="record_known", states={}
    )
    await compiler.async_compile(renamed, (start + timedelta(hours=2)).timestamp())

    assert await stored_name(hass, DURATION_OFF) == "Mains: off (h)"
    assert await stored_name(hass, COUNT_OFF) == "Mains: off (#)"
    assert await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=4)) == [
        0.5, 0.5, 0.5, 0.5
    ]


async def test_the_evidence_is_the_first_whole_hour_not_the_hour_of_the_row(
    recorder, freezer
):
    """The hour the oldest surviving row falls in is only partly known.

    Hour 2 was compiled while the rows before that row still existed.
    A recompute floored to the start of its hour would rebuild it from the
    carry alone - here a uniform hour 1 - and lose the spell inside it.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for minutes, state in ((0, "on"), (30, "off"), (140, "on"), (160, "off")):
        freezer.move_to(start + timedelta(minutes=minutes))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=3))
    assert on == pytest.approx([0.5, 0.5, 0.5 + 1 / 3])

    # Everything before 2:40 is purged; that row is the oldest evidence.
    freezer.move_to(start + timedelta(hours=2, minutes=30))
    await hass.services.async_call(
        "recorder", "purge", {"keep_days": 0}, blocking=True
    )
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await compiler.async_compile(cfg(), start.timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=4))
    assert on == pytest.approx([0.5, 0.5, 0.5 + 1 / 3, 0.5 + 1 / 3])
    assert off == pytest.approx([0.5, 1.5, 1.5 + 2 / 3, 2.5 + 2 / 3])


DURATION_UNAVAILABLE = (
    "discrete_statistics:binary_sensor_grid_status_unavailable_duration"
)
COUNT_UNAVAILABLE = "discrete_statistics:binary_sensor_grid_status_unavailable_count"


def short_cfg(min_duration=300.0):
    return EntityConfig(
        entity_id=ENTITY,
        name="Grid Status",
        default="record_known",
        states={"unavailable": "ignore_short"},
        min_duration=min_duration,
    )


async def _set_states(hass, freezer, start, rows):
    for seconds, state in rows:
        freezer.move_to(start + timedelta(seconds=seconds))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()


async def test_a_short_outage_is_carried_and_a_long_one_recorded(recorder, freezer):
    """A 20 s blip under a five-minute threshold, then a ten-minute one."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _set_states(
        hass,
        freezer,
        start,
        ((0, "on"), (600, "unavailable"), (620, "on"), (5400, "unavailable"), (6000, "on")),
    )

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(short_cfg(), start.timestamp())

    end = start + timedelta(hours=3)
    assert await read_sums(hass, DURATION_ON, start, end) == pytest.approx(
        [1.0, 2.0 - 600 / 3600, 3.0 - 600 / 3600]
    )
    assert await read_sums(hass, DURATION_UNAVAILABLE, start, end) == pytest.approx(
        [0.0, 600 / 3600, 600 / 3600]
    )
    assert await read_sums(hass, COUNT_UNAVAILABLE, start, end) == [0, 1, 1]
    assert await read_sums(hass, COUNT_ON, start, end) == [0, 1, 1]


async def test_a_spell_still_running_at_compile_time_is_settled_by_the_trailing_window(
    recorder, freezer
):
    """Unavailable from 0:59:50, compiled at 1:03 - too soon to know.

    The hour is compiled with the spell carried across, provisionally. It
    ends at 1:20, and the run at 2:03 recompiles hour 0 with the answer.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _set_states(hass, freezer, start, ((0, "on"), (3590, "unavailable")))

    freezer.move_to(start + timedelta(hours=1, minutes=3))
    compiler = Compiler(hass)
    await compiler.async_compile_incremental(short_cfg())
    assert await read_sums(
        hass, DURATION_ON, start, start + timedelta(hours=1)
    ) == [1.0]
    assert await existing(hass) == sorted([COUNT_ON, DURATION_ON])

    await _set_states(hass, freezer, start, ((4800, "on"),))
    freezer.move_to(start + timedelta(hours=2, minutes=3))
    await compiler.async_compile_incremental(short_cfg())

    end = start + timedelta(hours=2)
    assert await read_sums(hass, DURATION_ON, start, end) == pytest.approx(
        [1.0 - 10 / 3600, 2.0 - 1210 / 3600]
    )
    assert await read_sums(hass, DURATION_UNAVAILABLE, start, end) == pytest.approx(
        [10 / 3600, 1210 / 3600]
    )
    assert await read_sums(hass, COUNT_UNAVAILABLE, start, end) == [1, 1]


async def test_a_short_spell_across_a_chunk_seam_is_no_event(
    recorder, freezer, monkeypatch
):
    """The blip is dropped by both chunks and the second is handed `on`.

    Its first row is then into `on` - the state it was handed - twenty
    seconds after the seam, not on it.
    """
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 1)
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _set_states(
        hass, freezer, start, ((0, "on"), (3590, "unavailable"), (3620, "on"))
    )

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(short_cfg(), start.timestamp())

    end = start + timedelta(hours=3)
    assert await read_sums(hass, DURATION_ON, start, end) == [1.0, 2.0, 3.0]
    assert await read_sums(hass, COUNT_ON, start, end) == [0, 0, 0]
    assert await existing(hass) == sorted([COUNT_ON, DURATION_ON])


async def test_a_long_spell_across_a_chunk_seam_is_recorded_once(
    recorder, freezer, monkeypatch
):
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 1)
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _set_states(
        hass, freezer, start, ((0, "on"), (3000, "unavailable"), (3660, "on"))
    )

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(short_cfg(), start.timestamp())

    end = start + timedelta(hours=3)
    assert await read_sums(hass, DURATION_UNAVAILABLE, start, end) == pytest.approx(
        [600 / 3600, 660 / 3600, 660 / 3600]
    )
    assert await read_sums(hass, COUNT_UNAVAILABLE, start, end) == [1, 1, 1]
    assert await read_sums(hass, COUNT_ON, start, end) == [0, 1, 1]


async def test_ignore_short_as_the_default_debounces_end_to_end(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    debounced = EntityConfig(
        entity_id=ENTITY, name=None, default="ignore_short", min_duration=5.0
    )
    await _set_states(
        hass,
        freezer,
        start,
        (
            (0, "off"),
            (600, "on"),
            (600.2, "off"),
            (600.4, "on"),
            (600.6, "off"),
            (1200, "on"),
            (1800, "off"),
        ),
    )

    freezer.move_to(start + timedelta(hours=1))
    await Compiler(hass).async_compile(debounced, start.timestamp())

    end = start + timedelta(hours=1)
    assert await read_sums(hass, COUNT_ON, start, end) == [1]
    assert await read_sums(hass, COUNT_OFF, start, end) == [1]
    assert await read_sums(hass, DURATION_ON, start, end) == pytest.approx([600 / 3600])
