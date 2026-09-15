"""Tests for the compiler against a real recorder."""

import functools as ft
import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsMeta
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.purge import purge_old_data
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.components.recorder.util import session_scope
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from custom_components.discrete_statistics import compiler as compiler_module
from custom_components.discrete_statistics.compiler import TRAILING_HOURS, Compiler
from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import (
    HOUR,
    METRIC_COUNT,
    METRIC_DURATION,
)
from custom_components.discrete_statistics.payload import metadata_for
from custom_components.discrete_statistics.statistic_ids import parse
from tests.conftest import T0, existing, play, read_sums

ENTITY = "binary_sensor.grid_status"
DURATION_OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"
COUNT_OFF = "discrete_statistics:binary_sensor_grid_status_off_count"
DURATION_ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
COUNT_ON = "discrete_statistics:binary_sensor_grid_status_on_count"
DURATION_UNKNOWN = "discrete_statistics:binary_sensor_grid_status_unknown_duration"
DURATION_MISSING = "discrete_statistics:binary_sensor_grid_status_missing_duration"
COUNT_MISSING = "discrete_statistics:binary_sensor_grid_status_missing_count"
TEMP = "binary_sensor.grid_status_2"
NEW = "binary_sensor.grid_status_new"
NEW_OFF = "discrete_statistics:binary_sensor_grid_status_new_off_duration"
NEW_ON = "discrete_statistics:binary_sensor_grid_status_new_on_duration"


def cfg():
    return EntityConfig(
        entity_id=ENTITY, name="Grid Status", default="record_known", states={}
    )


async def stored_name(hass, statistic_id):
    await get_instance(hass).async_block_till_done()
    metadata = await get_instance(hass).async_add_executor_job(
        ft.partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    return metadata[statistic_id][1]["name"]


async def read_rows(hass, statistic_id, start, end):
    """The rows a statistic holds in [start, end), as (hour, sum)."""
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
    return [(row["start"], row["sum"]) for row in result.get(statistic_id, [])]


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

    sums = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=2))
    assert sums[0] == pytest.approx(0.25)
    counts = await read_sums(hass, COUNT_OFF, start, start + timedelta(hours=2))
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
    all_at_once = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=5))
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
    stepwise = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=5))

    assert all_at_once == stepwise


async def test_the_trailing_window_picks_up_a_late_committed_state(recorder, freezer):
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


async def test_recompiling_back_before_the_first_state_writes_no_gap(recorder, freezer):
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
    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
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

    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
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

    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
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
    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
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


async def test_hours_that_cannot_be_recomputed_are_left_as_they_are(recorder, freezer):
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
    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
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
    await compiler.async_compile(cfg(), (start + timedelta(hours=1)).timestamp())
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
    from_earlier = await read_sums(hass, COUNT_OFF, start, start + timedelta(hours=4))
    assert from_earlier == [0, 0, 1, 1]

    # Recompile a window that begins exactly on the transition.
    await compiler.async_compile(cfg(), (start + timedelta(hours=2)).timestamp())
    from_boundary = await read_sums(hass, COUNT_OFF, start, start + timedelta(hours=4))

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


async def test_a_new_entity_opens_at_the_first_whole_hour_it_knows(recorder, freezer):
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
    assert await read_rows(hass, DURATION_ON, start, start + timedelta(hours=6)) == []
    # The surviving state carries on undisturbed and monotonic. Hour 0 was
    # spent entirely in "on", so deleting it leaves no row there at all.
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=6))
    assert off == [1.0, 2.0, 3.0, 4.0, 5.0]


async def test_deleting_one_metric_sticks_until_its_state_recurs(recorder, freezer):
    """Deletion is per statistic, not per state.

    Keyed by state, the surviving count would keep "on" alive and undo
    the deletion on the very next compile. Keyed by statistic it sticks -
    until "on" actually happens again, at which point an observed state is
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


async def test_a_quiet_state_writes_no_rows_and_the_hours_still_tile(recorder, freezer):
    """A state absent from an hour gets no row there.

    The states present do, and their changes sum to the hour. Asserted on
    rows, not on ID membership: nothing ever removes a statistics_meta row,
    so membership proves nothing.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)

    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile_incremental(cfg())

    end = start + timedelta(hours=6)
    on = await read_rows(hass, DURATION_ON, start, end)
    off = await read_rows(hass, DURATION_OFF, start, end)
    assert on == [(start.timestamp(), 1.0)]
    assert [(h - start.timestamp()) / HOUR for h, _ in off] == [1, 2, 3, 4, 5]
    assert [s for _, s in off] == [1.0, 2.0, 3.0, 4.0, 5.0]

    # Every compiled hour is tiled by the duration rows present in it.
    change = {}
    for sid, rows in ((DURATION_ON, on), (DURATION_OFF, off)):
        previous = 0.0
        for hour, total in rows:
            change.setdefault(hour, 0.0)
            change[hour] += total - previous
            previous = total
    assert sorted(change) == [start.timestamp() + h * HOUR for h in range(6)]
    assert all(total == pytest.approx(1.0) for total in change.values())


async def test_a_statistic_created_in_one_chunk_continues_its_sum_in_a_later_one(
    recorder, freezer, monkeypatch
):
    """The sums thread across the chunk seam.

    A statistic first written in chunk N is still queued when chunk N+2
    sees its state again, so a base read from the recorder would find
    nothing and restart it at zero. The chunk hands its sums forward.
    """
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 2)

    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=4, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    compiler = Compiler(hass)
    assert await compiler.async_compile(cfg(), start.timestamp()) == 6

    on = await read_rows(hass, DURATION_ON, start, start + timedelta(hours=6))
    assert on == [
        (start.timestamp(), 0.5),
        (start.timestamp() + 4 * HOUR, 1.0),
        (start.timestamp() + 5 * HOUR, 2.0),
    ]


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


async def test_two_entities_never_write_into_each_others_statistics(recorder, freezer):
    """`belongs_to` is what keeps them apart, and it is load-bearing.

    The entity IDs are chosen so one slug is a prefix of the other at an
    underscore boundary - the case a multi-token state would collide on.
    A filter that is too broad would have each compile write rows into
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
        0,
        0,
        0,
        0,
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


async def test_a_reloaded_entity_is_recorded_as_unknown_under_record(recorder, freezer):
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

    unknown = await read_sums(hass, DURATION_UNKNOWN, start, start + timedelta(hours=4))
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


UNNAMED = EntityConfig(entity_id=ENTITY, name=None, default="record_known", states={})


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
        "binary_sensor",
        "demo",
        "unique-1",
        suggested_object_id="grid_status",
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
        "binary_sensor",
        "demo",
        "unique-2",
        suggested_object_id="grid_status",
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
        "binary_sensor",
        "demo",
        "door-1",
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
    # Both states have a row from hour 1 on, and every hour still totals
    # wall-clock time.
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

    assert (
        await stored_name(
            hass, "discrete_statistics:binary_sensor_grid_status_unavailable_duration"
        )
        == "Grid Status: Unavailable (h)"
    )
    assert await stored_name(hass, DURATION_UNKNOWN) == "Grid Status: Unknown (h)"


async def test_a_state_older_than_the_purge_horizon_is_still_carried(recorder, freezer):
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
    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
    await get_instance(hass).async_block_till_done()
    # Nothing left in the recorder, so the opening moment now comes from the
    # live state: it began exactly on the hour, so that hour is usable whole.
    assert await Compiler(hass).async_earliest_state_ts(ENTITY) == start.timestamp()

    await Compiler(hass).async_compile(cfg(), (start + timedelta(hours=1)).timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    counts = await read_sums(hass, COUNT_ON, start, start + timedelta(hours=4))
    assert on == [1.0, 2.0, 3.0]
    assert counts == [0, 0, 0]


async def test_a_state_that_began_inside_the_window_is_not_carried(recorder, freezer):
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
    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
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
    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
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
    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
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
    await Compiler(hass).async_compile(cfg(), (start + timedelta(hours=2)).timestamp())

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


async def test_a_standing_row_with_no_change_does_not_vouch_for_its_state(
    recorder, freezer
):
    """Carry source 4 reads change, not presence.

    A row rewritten with the carried sum stands in the hour but says the
    state had no time in it. Counting it would find two statistics in the
    hour and refuse to carry - leaving the window uncompiled.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    # Unavailable is ignored under record_known: the recorder and the
    # state machine both fall silent from here, so hour 2 can only be
    # opened from the statistics of hour 1.
    freezer.move_to(start + timedelta(minutes=45))
    hass.states.async_set(ENTITY, "unavailable")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)
    # A stale "on" row in hour 1, carrying the sum with nothing added.
    async_add_external_statistics(
        hass,
        metadata_for(METRIC_DURATION, DURATION_ON, "Grid Status: on (h)"),
        [{"start": start + timedelta(hours=1), "sum": 0.5}],
    )
    await async_wait_recording_done(hass)

    freezer.move_to(start + timedelta(hours=4))
    assert (
        await compiler.async_compile(cfg(), (start + timedelta(hours=2)).timestamp())
        == 2
    )

    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=4))
    assert off == [0.5, 1.5, 2.5, 3.5]


async def test_a_recompile_that_drops_a_state_from_an_hour_rewrites_its_row(
    recorder, freezer
):
    """Nothing deletes, so a stale row is corrected by rewriting it.

    Without the rewrite the stale sum stands ahead of every later row and
    the series stops being monotonic.
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

    # A row for "on" in hour 1 that the history does not support.
    async_add_external_statistics(
        hass,
        metadata_for(METRIC_DURATION, DURATION_ON, "Grid Status: on (h)"),
        [
            {"start": start, "sum": 0.5},
            {"start": start + timedelta(hours=1), "sum": 0.9},
        ],
    )
    await async_wait_recording_done(hass)

    freezer.move_to(start + timedelta(hours=3))
    await Compiler(hass).async_compile(cfg(), start.timestamp())

    on = await read_rows(hass, DURATION_ON, start, start + timedelta(hours=3))
    assert on == [(start.timestamp(), 0.5), (start.timestamp() + HOUR, 0.5)]


@pytest.fixture
def all_statements(recorder):
    """Every SQL statement the recorder's engine runs, with its parameters.

    Not `conftest.statements`, which is the statistics-table SELECTs alone.
    """
    seen: list[tuple[str, tuple]] = []

    def listen(conn, cursor, statement, parameters, context, executemany):
        flat = parameters if executemany else [parameters]
        seen.append((statement, tuple(p for group in flat for p in (group or ()))))

    engine = get_instance(recorder).engine
    sqlalchemy_event.listen(engine, "before_cursor_execute", listen)
    yield seen
    sqlalchemy_event.remove(engine, "before_cursor_execute", listen)


async def test_the_base_of_a_quiet_statistic_is_a_seek_not_a_scan(
    recorder, freezer, all_statements
):
    """A statistic with rows on both sides of the window costs one seek.

    A recompute inside the series is where a read from the epoch would
    hide - and under sparse rows that is every inactive statistic on
    every chunk of a backfill. Every SELECT over the statistics table
    during the compile binds nothing older than the window's neighbourhood.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)
    # "on" again at hour 20, so its newest row is ahead of a window at 10.
    freezer.move_to(start + timedelta(hours=20))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=20, minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=30))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)

    all_statements.clear()
    window = start + timedelta(hours=10)
    await compiler.async_compile(
        cfg(), window.timestamp(), (window + timedelta(hours=2)).timestamp()
    )
    await async_wait_recording_done(hass)

    floor = (window - timedelta(hours=1)).timestamp()
    selects = [
        (sql, params)
        for sql, params in all_statements
        if sql.lstrip().upper().startswith("SELECT")
        and re.search(r"\bstatistics\b", sql)
    ]
    assert selects, "nothing was read"
    packed = 0
    for sql, params in selects:
        edges = list(_bounds(params))
        packed += sum(1 for p in params if isinstance(p, str) and p.startswith("[["))
        assert all(p >= floor for p in edges), (sql, params)
    # Without a pair list read, the check above would pass on a statement
    # whose edges are all packed inside one parameter it never unpacked.
    assert packed, "no JSON pair list was seen"


def _bounds(params):
    """The timestamps a statement binds, JSON pair lists unpacked.

    `rows.rows_before` hands the (metadata_id, edge) pairs to SQLite as one
    JSON string, so the edges are inside a parameter rather than being
    parameters - and a read from the epoch would hide there.
    """
    for param in params:
        if isinstance(param, float):
            yield param
        elif isinstance(param, str) and param.startswith("[["):
            for _, edge in json.loads(param):
                yield edge


async def test_recompiling_unchanged_history_writes_no_rows(
    recorder, freezer, all_statements
):
    """Every row already stands with the sum this compile computes.

    A recompute is almost always over history that has not changed, and
    the recorder spends two statements on every row handed to it - so the
    rows it is handed are the whole cost.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)

    all_statements.clear()
    await compiler.async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)

    written = [
        sql
        for sql, _ in all_statements
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE"))
        and re.search(r"\bstatistics\b", sql)
    ]
    assert written == []
    # The second compile still agrees with the first.
    assert await read_rows(hass, DURATION_OFF, start, start + timedelta(hours=4)) == [
        (start.timestamp() + HOUR, 1.0),
        (start.timestamp() + 2 * HOUR, 2.0),
        (start.timestamp() + 3 * HOUR, 3.0),
    ]


def _imports(monkeypatch):
    """The statistic ids handed to `async_add_external_statistics`."""
    seen: list[str] = []
    real = compiler_module.async_add_external_statistics

    def record(hass, metadata, rows):
        seen.append(metadata["statistic_id"])
        return real(hass, metadata, rows)

    monkeypatch.setattr(compiler_module, "async_add_external_statistics", record)
    return seen


async def test_a_quiet_statistic_with_an_unchanged_name_is_not_imported(
    recorder, freezer, monkeypatch
):
    """Metadata the recorder already holds, and no rows: nothing to say.

    An import is a task, a commit and a metadata round trip on a queue
    every integration shares.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(
        cfg(), start.timestamp(), (start + timedelta(hours=2)).timestamp()
    )
    await async_wait_recording_done(hass)

    seen = _imports(monkeypatch)
    await compiler.async_compile(
        cfg(),
        (start + timedelta(hours=2)).timestamp(),
        (start + timedelta(hours=4)).timestamp(),
    )
    await async_wait_recording_done(hass)

    # "on" is over, and "off" is entered nowhere in the window: only its
    # duration has a row to write, and that row is new, so it goes by the
    # bulk path and not by an import.
    assert seen == []
    # And that row was written all the same.
    assert await read_rows(hass, DURATION_OFF, start, start + timedelta(hours=4)) == [
        (start.timestamp() + HOUR, 1.0),
        (start.timestamp() + 2 * HOUR, 2.0),
        (start.timestamp() + 3 * HOUR, 3.0),
    ]


def _queue_spy(monkeypatch, hass, *, swallow_fence=False):
    """Record the task types the compile queues, optionally dropping the fence."""
    instance = get_instance(hass)
    real = instance.queue_task
    seen: list[str] = []

    def queue_task(task):
        seen.append(type(task).__name__)
        if swallow_fence and isinstance(task, SynchronizeTask):
            return
        real(task)

    monkeypatch.setattr(instance, "queue_task", queue_task)
    return seen


async def test_a_recorder_that_is_not_running_is_not_fenced(
    recorder, freezer, monkeypatch
):
    """Nothing would serve the task, and the await is inside the compile lock.

    A fence that never resolves there stalls every later hourly run and
    every `recompute` for the life of the process.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    seen = _queue_spy(monkeypatch, hass)
    monkeypatch.setattr(get_instance(hass), "is_running", False)
    freezer.move_to(start + timedelta(hours=4))
    assert await Compiler(hass).async_compile(cfg(), start.timestamp())

    assert "SynchronizeTask" not in seen
    # The rows were still handed over, which is what the fence waits for.
    assert "ImportStatisticsTask" in seen


async def test_a_fence_that_never_commits_gives_up_and_warns(
    recorder, freezer, monkeypatch, caplog
):
    """A compile that has enqueued its rows must not fail over a late fence."""
    # Zero rather than a short wait: these tests run under a frozen clock,
    # where no positive bound ever elapses. A deadline already reached
    # fires on the next pass of the loop either way.
    monkeypatch.setattr(compiler_module, "FENCE_TIMEOUT", 0)
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    seen = _queue_spy(monkeypatch, hass, swallow_fence=True)
    freezer.move_to(start + timedelta(hours=4))
    assert await Compiler(hass).async_compile(cfg(), start.timestamp())

    assert "SynchronizeTask" in seen
    assert "Timed out waiting" in caplog.text


async def test_a_rowless_statistic_whose_metadata_drifted_is_imported(
    recorder, freezer, monkeypatch
):
    """The name is not the only field a compile sets.

    A statistic the window never sees must still have its unit, its unit
    class and its mean type brought to what this version writes - the
    recorder rewrites the metadata row, and only an import reaches it.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(
        cfg(), start.timestamp(), (start + timedelta(hours=2)).timestamp()
    )
    await async_wait_recording_done(hass)

    # A mean where this version writes none, and the same name: the older
    # scheme, on a state the next window has nothing to say about.
    drifted = {**metadata_for(METRIC_DURATION, DURATION_ON, "Grid Status: on (h)")}
    drifted.update(has_mean=True, mean_type=StatisticMeanType.ARITHMETIC)
    async_add_external_statistics(hass, drifted, [])
    await async_wait_recording_done(hass)

    seen = _imports(monkeypatch)
    await compiler.async_compile(
        cfg(),
        (start + timedelta(hours=2)).timestamp(),
        (start + timedelta(hours=4)).timestamp(),
    )
    await async_wait_recording_done(hass)

    assert DURATION_ON in seen
    metadata = await get_instance(hass).async_add_executor_job(
        ft.partial(get_metadata, hass, statistic_ids={DURATION_ON})
    )
    assert metadata[DURATION_ON][1]["mean_type"] is StatisticMeanType.NONE


async def test_a_renamed_statistic_is_imported_even_with_no_rows(
    recorder, freezer, monkeypatch
):
    """The relabel is the one reason a rowless payload is still imported."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    await compiler.async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)

    seen = _imports(monkeypatch)
    renamed = EntityConfig(
        entity_id=ENTITY, name="The Grid", default="record_known", states={}
    )
    await compiler.async_compile(renamed, start.timestamp())
    await async_wait_recording_done(hass)

    assert set(seen) == {DURATION_ON, COUNT_ON, DURATION_OFF, COUNT_OFF}
    assert await stored_name(hass, DURATION_ON) == "The Grid: on (h)"


async def test_a_chunk_that_raises_still_drains_the_ones_before_it(
    recorder, freezer, monkeypatch
):
    """The writes of the chunks before it are committed, not left queued.

    The standing rows and the metadata are read live, so a compile that
    returned with rows still queued would let the next one see half of them.
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
    assert [row["sum"] for row in result[DURATION_ON]] == [0.5]
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
    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await compiler.async_compile(cfg(), (start + timedelta(hours=2)).timestamp())

    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=4))
    assert on == [0.5, 0.5, 1.5, 2.5]
    assert off == [0.5, 1.5, 1.5, 1.5]


async def test_a_rename_reaches_an_absent_state_through_the_compiler(recorder, freezer):
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
        0.5,
        0.5,
        0.5,
        0.5,
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
    await hass.services.async_call("recorder", "purge", {"keep_days": 0}, blocking=True)
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
        (
            (0, "on"),
            (600, "unavailable"),
            (620, "on"),
            (5400, "unavailable"),
            (6000, "on"),
        ),
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
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=1)) == [
        1.0
    ]
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


async def test_dense_rows_already_written_are_carried_across(recorder, freezer):
    """A database holding a row per state per hour keeps working.

    Those rows stand, so they are rewritten wherever a window covers them;
    nothing is deleted; the metadata loses its mean on the next compile.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    # A row every hour, with a mean: the scheme that came before.
    dense = {**metadata_for(METRIC_DURATION, DURATION_ON, "Grid Status: on (h)")}
    dense.update(has_mean=True, mean_type=StatisticMeanType.ARITHMETIC)
    async_add_external_statistics(
        hass,
        dense,
        [
            # Hours 1-3 carry a wrong sum, so the rewrite is observable.
            {
                "start": start + timedelta(hours=h),
                "sum": 1.0 if h == 0 else 0.9,
                "mean": 0.0,
                "min": 0.0,
                "max": 0.0,
            }
            for h in range(4)
        ],
    )
    await async_wait_recording_done(hass)

    freezer.move_to(start + timedelta(hours=6))
    await Compiler(hass).async_compile(cfg(), start.timestamp())

    on = await read_rows(hass, DURATION_ON, start, start + timedelta(hours=6))
    # Hours 0-3 stood and are rewritten; hours 4-5 never held a row.
    assert on == [(start.timestamp() + h * HOUR, 1.0) for h in range(4)]
    metadata = await get_instance(hass).async_add_executor_job(
        ft.partial(get_metadata, hass, statistic_ids={DURATION_ON})
    )
    assert metadata[DURATION_ON][1]["mean_type"] is StatisticMeanType.NONE


async def test_a_statistic_whose_only_row_is_the_previous_hour_vouches_for_it(
    recorder, freezer
):
    """Carry source 4 reads the hour's value, and a first row's value is its sum.

    With no earlier row to subtract, the base is zero: taking the newest
    row's own sum instead would make the hour read as unchanged and leave
    the window with no state to open in.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    # Ignored under record_known: from here neither the recorder nor the
    # state machine can open a later window.
    freezer.move_to(start + timedelta(minutes=45))
    hass.states.async_set(ENTITY, "unavailable")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    # Only hour 1 is compiled, so "on" holds exactly one row.
    freezer.move_to(start + timedelta(hours=2))
    compiler = Compiler(hass)
    await compiler.async_compile(
        cfg(),
        (start + timedelta(hours=1)).timestamp(),
        (start + timedelta(hours=2)).timestamp(),
    )
    await async_wait_recording_done(hass)
    assert await read_rows(hass, DURATION_ON, start, start + timedelta(hours=2)) == [
        ((start + timedelta(hours=1)).timestamp(), 1.0)
    ]

    freezer.move_to(start + timedelta(hours=3))
    assert (
        await compiler.async_compile(cfg(), (start + timedelta(hours=2)).timestamp())
        == 1
    )

    assert await read_sums(
        hass, DURATION_ON, start + timedelta(hours=1), start + timedelta(hours=3)
    ) == [1.0, 2.0]


def _paths(monkeypatch, hass):
    """What each write path was handed, and the order they were queued in.

    Two lists of `(statistic_id, [hour, ...])` - the imports and the bulk
    task's payloads - plus one list of labels in queue order, which is
    what proves a statistic's metadata reaches the recorder before its
    rows do.
    """
    imports: list[tuple[str, list[float]]] = []
    bulk: list[tuple[str, list[float]]] = []
    order: list[str] = []
    real_import = compiler_module.async_add_external_statistics

    def record(hass_, metadata, rows):
        statistic_id = metadata["statistic_id"]
        order.append(f"import {statistic_id}")
        imports.append((statistic_id, [row["start"].timestamp() for row in rows]))
        return real_import(hass_, metadata, rows)

    monkeypatch.setattr(compiler_module, "async_add_external_statistics", record)

    instance = get_instance(hass)
    real_queue = instance.queue_task

    def queue_task(task):
        if isinstance(task, compiler_module.BulkInsertTask):
            for statistic_id, (_, rows) in task.payloads.items():
                order.append(f"bulk {statistic_id}")
                bulk.append((statistic_id, [row["start"].timestamp() for row in rows]))
        return real_queue(task)

    monkeypatch.setattr(instance, "queue_task", queue_task)
    return imports, bulk, order


async def test_new_rows_are_inserted_and_only_differing_rows_are_imported(
    recorder, freezer, monkeypatch
):
    """The three cases an hour can be in, and the path each takes.

    No row stands: the row is new and nothing else could have written it,
    so it is inserted in bulk. A row stands with a different sum: it is an
    upsert and goes through the recorder's own import, which is the only
    thing that knows how to do one. A row stands with the same sum:
    neither, as `lean-writes` established.
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

    # Hour 0 stands with the sum the compile will reach; hour 1 stands
    # with one the history does not support.
    async_add_external_statistics(
        hass,
        metadata_for(METRIC_DURATION, DURATION_ON, "Grid Status: on (h)"),
        [
            {"start": start, "sum": 0.5},
            {"start": start + timedelta(hours=1), "sum": 0.9},
        ],
    )
    await async_wait_recording_done(hass)

    freezer.move_to(start + timedelta(hours=3))
    imports, bulk, _ = _paths(monkeypatch, hass)
    await Compiler(hass).async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)

    hour = start.timestamp()
    # Only the differing standing row is imported with rows beside it.
    assert (DURATION_ON, [hour + HOUR]) in imports
    assert [rows for sid, rows in imports if sid == DURATION_ON] == [[hour + HOUR]]
    # Every other row of the window is new.
    assert sorted(bulk) == [
        (COUNT_OFF, [hour]),
        (DURATION_OFF, [hour, hour + HOUR, hour + 2 * HOUR]),
    ]
    # And the database holds what both paths were asked for: hour 0's
    # standing row untouched, hour 1's corrected, the new rows inserted.
    assert await read_rows(hass, DURATION_ON, start, start + timedelta(hours=3)) == [
        (hour, 0.5),
        (hour + HOUR, 0.5),
    ]
    assert await read_rows(hass, DURATION_OFF, start, start + timedelta(hours=3)) == [
        (hour, 0.5),
        (hour + HOUR, 1.5),
        (hour + 2 * HOUR, 2.5),
    ]
    assert await read_rows(hass, COUNT_OFF, start, start + timedelta(hours=3)) == [
        (hour, 1.0)
    ]


# Every column a row carries but the ones that legitimately differ
# between two builds: the row id, and `created`/`created_ts`, which are
# the time the row was written.
_ROW_COLUMNS = (
    "start_ts",
    "start",
    "sum",
    "mean",
    "mean_weight",
    "min",
    "max",
    "state",
    "last_reset",
    "last_reset_ts",
)


async def _raw_rows(hass, statistic_id, start, end):
    """Every column of every row, straight from the table.

    `statistics_during_period` returns the fields it was asked for, so a
    stray non-None `mean` or `state` from the bulk path would never show.
    """

    def read():
        with session_scope(hass=hass, read_only=True) as session:
            metadata_id = session.execute(
                select(StatisticsMeta.id).where(
                    StatisticsMeta.statistic_id == statistic_id
                )
            ).scalar()
            return [
                tuple(row)
                for row in session.execute(
                    select(*(getattr(Statistics, name) for name in _ROW_COLUMNS))
                    .where(Statistics.metadata_id == metadata_id)
                    .where(Statistics.start_ts >= start.timestamp())
                    .where(Statistics.start_ts < end.timestamp())
                    .order_by(Statistics.start_ts)
                ).all()
            ]

    await get_instance(hass).async_block_till_done()
    return await get_instance(hass).async_add_executor_job(read)


async def _snapshot(hass, start, end):
    """Every row of every statistic the entity has, by statistic."""
    return {
        statistic_id: await _raw_rows(hass, statistic_id, start, end)
        for statistic_id in sorted(await existing(hass))
    }


async def _seed_a_varied_history(hass, freezer, start):
    """Several hours, several states, one of them appearing only late on."""
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    for minutes, state in (
        (30, "off"),
        (95, "on"),
        (150, "off"),
        (185, "on"),
        (260, "off"),
        # A state - and so a statistic - that appears only in a later chunk.
        (300, "missing"),
        (330, "off"),
    ):
        freezer.move_to(start + timedelta(minutes=minutes))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()


async def test_a_rebuild_writes_the_same_rows_with_or_without_the_bulk_path(
    recorder, freezer, monkeypatch
):
    """The equivalence the whole spike rests on.

    A build from empty statistics, compiled once through the recorder's
    import and once through the bulk insert, must leave the database
    holding exactly the same rows - across a chunk seam, and including a
    statistic created in the second chunk.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 2)
    await _seed_a_varied_history(hass, freezer, start)
    end = start + timedelta(hours=7)
    freezer.move_to(end)
    compiler = Compiler(hass)

    monkeypatch.setattr(compiler_module, "BULK_INSERT", False)
    await compiler.async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)
    imported = await _snapshot(hass, start, end)
    assert len(imported) >= 6
    assert any(rows for rows in imported.values())

    await _delete(hass, list(imported))
    await async_wait_recording_done(hass)
    assert await existing(hass) == []

    monkeypatch.setattr(compiler_module, "BULK_INSERT", True)
    _, bulk, _ = _paths(monkeypatch, hass)
    await compiler.async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)

    # Worth nothing unless the second build really took the bulk path.
    assert any(rows for _, rows in bulk)
    assert await _snapshot(hass, start, end) == imported


async def test_a_statistics_metadata_is_queued_before_its_bulk_rows(
    recorder, freezer, monkeypatch
):
    """The bulk task resolves metadata ids itself, so the row must exist.

    Both go on the recorder's queue and it is served in order, so the
    import that creates a new `statistics_meta` row has to be queued
    first. It is: the chunk imports inside its loop over the payloads and
    queues the one bulk task after it.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    _, _, order = _paths(monkeypatch, hass)
    await Compiler(hass).async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)

    for statistic_id in (DURATION_ON, DURATION_OFF, COUNT_OFF):
        assert order.index(f"import {statistic_id}") < order.index(
            f"bulk {statistic_id}"
        )
    # Which is worth nothing unless the rows arrived.
    assert await read_rows(hass, DURATION_OFF, start, start + timedelta(hours=4)) == [
        (start.timestamp() + HOUR, 1.0),
        (start.timestamp() + 2 * HOUR, 2.0),
        (start.timestamp() + 3 * HOUR, 3.0),
    ]


async def test_the_fence_covers_the_bulk_task(recorder, freezer, monkeypatch):
    """A compile milliseconds behind reads the bulk rows in its base.

    The bulk task is queued ahead of `SynchronizeTask`, so it has
    committed by the time `async_compile` returns. Without that the next
    compile's base would read zero and its sums would restart there.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    compiler = Compiler(hass)
    queued = _queue_spy(monkeypatch, hass)
    await compiler.async_compile(
        cfg(), start.timestamp(), (start + timedelta(hours=2)).timestamp()
    )
    # The queue is served in order, so this is the whole guarantee.
    assert queued.index("BulkInsertTask") < queued.index("SynchronizeTask")
    # No wait: the fence is the only thing that has happened.
    await compiler.async_compile(
        cfg(),
        (start + timedelta(hours=2)).timestamp(),
        (start + timedelta(hours=4)).timestamp(),
    )

    # Hour 0 is "on". Without the fence covering the bulk task the second
    # compile's base would read zero and hours 2 and 3 would restart there.
    assert await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=4)) == [
        pytest.approx(0.0),
        pytest.approx(1.0),
        pytest.approx(2.0),
        pytest.approx(3.0),
    ]


async def _rows_without_waiting(hass, statistic_id, start, end):
    """`read_rows` without the queue drain, so a late write cannot hide."""
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
    return [(row["start"], row["sum"]) for row in result.get(statistic_id, [])]


async def test_a_failed_bulk_insert_falls_back_inside_the_fence(
    recorder, freezer, monkeypatch, caplog
):
    """The fallback runs on the recorder thread, not on the queue behind it.

    `_async_fence` is queued after the chunk, so a fallback that queued a
    task of its own would commit after `async_compile` returned - and the
    coordinator refreshing on `compiled_signal` would read rows that are
    not there yet.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)
    freezer.move_to(start + timedelta(hours=4))

    def collide(instance, payloads):
        raise IntegrityError("INSERT INTO statistics", {}, Exception("UNIQUE"))

    monkeypatch.setattr(compiler_module, "_bulk_insert", collide)
    queued = _queue_spy(monkeypatch, hass)
    await Compiler(hass).async_compile(cfg(), start.timestamp())

    assert "falling back to the recorder's own import" in caplog.text
    # Nothing carrying rows was queued behind the fence.
    behind = queued[queued.index("SynchronizeTask") + 1 :]
    assert "ImportStatisticsTask" not in behind
    assert "BulkInsertTask" not in behind
    # And the rows are readable with no wait beyond the compile's own fence.
    assert await _rows_without_waiting(
        hass, DURATION_OFF, start, start + timedelta(hours=4)
    ) == [
        (start.timestamp() + HOUR, 1.0),
        (start.timestamp() + 2 * HOUR, 2.0),
        (start.timestamp() + 3 * HOUR, 3.0),
    ]


async def test_a_bulk_task_without_metadata_warns_and_writes_nothing(recorder, caplog):
    """The statistic was deleted while the compile ran.

    The import that would have created it is ahead of this task in the
    same queue and retries itself, so no metadata here means gone rather
    than late - and nothing else records that a statistic was deleted, so
    recreating it would resurrect it.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    task = compiler_module.BulkInsertTask(
        {
            DURATION_OFF: (
                metadata_for(METRIC_DURATION, DURATION_OFF, "Grid Status: off (h)"),
                [{"start": start, "sum": 1.0}],
            )
        }
    )
    get_instance(hass).queue_task(task)
    await async_wait_recording_done(hass)

    assert f"No metadata for {DURATION_OFF}" in caplog.text
    assert await existing(hass) == []


async def test_a_bulk_row_that_collides_falls_back_to_the_import(
    recorder, freezer, caplog
):
    """A row we called new turning out to stand must not be lost.

    We believe it cannot happen - the standing read and this write are one
    compile under one lock, and nothing else writes a
    `discrete_statistics:` statistic - but dropping the batch would leave
    every later sum standing on a base that was never written.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)
    freezer.move_to(start + timedelta(hours=4))
    await Compiler(hass).async_compile(cfg(), start.timestamp())
    await async_wait_recording_done(hass)

    # Every one of these hours already has a row.
    get_instance(hass).queue_task(
        compiler_module.BulkInsertTask(
            {
                DURATION_OFF: (
                    metadata_for(METRIC_DURATION, DURATION_OFF, "Grid Status: off (h)"),
                    [
                        {"start": start + timedelta(hours=1), "sum": 9.0},
                        {"start": start + timedelta(hours=2), "sum": 9.5},
                    ],
                )
            }
        )
    )
    await async_wait_recording_done(hass)

    assert "falling back to the recorder's own import" in caplog.text
    # The upsert ran: the rows carry the sums the task was handed.
    assert await read_rows(hass, DURATION_OFF, start, start + timedelta(hours=4)) == [
        (start.timestamp() + HOUR, 9.0),
        (start.timestamp() + 2 * HOUR, 9.5),
        (start.timestamp() + 3 * HOUR, 3.0),
    ]


async def test_a_compile_can_read_another_entitys_history(recorder_utc, freezer):
    """`read_from` moves only the history reads; the IDs stay ours."""
    hass = recorder_utc
    await play(
        hass,
        freezer,
        [(T0, "on"), (T0 + timedelta(hours=2), "off")],
        entity_id=TEMP,
    )
    freezer.move_to(T0 + timedelta(hours=4))
    hours = await compiler_module.Compiler(hass).async_compile(
        cfg(), T0.timestamp(), (T0 + timedelta(hours=4)).timestamp(), read_from=TEMP
    )
    await async_wait_recording_done(hass)

    assert hours == 4
    # `on` is carried into the window rather than entered, so it gets no
    # count row of its own - but `build_payloads` still plans and imports
    # COUNT_ON's metadata, as it does for every state that appears at all,
    # whether or not it was ever counted.
    assert await existing(hass) == sorted(
        [DURATION_ON, DURATION_OFF, COUNT_ON, COUNT_OFF]
    )
    assert await existing(hass, TEMP) == []
    assert await read_sums(hass, DURATION_ON, T0, T0 + timedelta(hours=4)) == [
        1.0,
        2.0,
        2.0,
        2.0,
    ]


async def test_read_from_reaches_the_evidence_and_the_opening_floor(
    recorder_utc, freezer
):
    """`read_from` moves the evidence and the opening floor with it.

    The source has no recorded history until two hours in - the earlier
    row is purged, which is what moves the floor to T0+2h - and no live
    state either can vouch earlier than that, so the window must move to
    the source's first whole hour instead of opening at T0.
    """
    hass = recorder_utc
    freezer.move_to(T0 - timedelta(days=2))
    hass.states.async_set(TEMP, "on")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    # Purging the row this wrote is what moves the floor to T0+2h: the
    # source's earliest retained evidence is then the `off` row below.
    await get_instance(hass).async_add_executor_job(
        purge_old_data, get_instance(hass), T0 - timedelta(days=1), False
    )
    await async_wait_recording_done(hass)
    await play(hass, freezer, [(T0 + timedelta(hours=2), "off")], entity_id=TEMP)

    freezer.move_to(T0 + timedelta(hours=4))
    hours = await compiler_module.Compiler(hass).async_compile(
        cfg(), T0.timestamp(), (T0 + timedelta(hours=4)).timestamp(), read_from=TEMP
    )
    await async_wait_recording_done(hass)

    # Two hours, not four: nothing vouched for T0 and T0+1h.
    assert hours == 2
    assert await read_sums(hass, DURATION_OFF, T0, T0 + timedelta(hours=4)) == [
        1.0,
        2.0,
    ]


async def test_the_state_machine_carry_is_asked_for_our_entity_not_the_read_one(
    recorder_utc, freezer
):
    """Our own live state opens the window, never the read identity's.

    ENTITY has been `on` since before the window opens; TEMP's only
    recorded row lands exactly on T0, a transition `canonicalise` cannot
    fold into a carry, and `first_whole_hour(T0) == T0` so the opening
    floor never intervenes - the carry decision is genuinely reached.
    The window must open carried in ENTITY's own `on` and count the `off`
    row as an entry. Asking TEMP's live state instead would find `off`
    with `last_changed == T0`, which the guard accepts and which
    `_open_window` then dedupes against the row itself - losing the count
    entirely.
    """
    hass = recorder_utc
    freezer.move_to(T0 - timedelta(hours=1))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    await play(hass, freezer, [(T0, "off")], entity_id=TEMP)

    freezer.move_to(T0 + timedelta(hours=2))
    hours = await compiler_module.Compiler(hass).async_compile(
        cfg(), T0.timestamp(), (T0 + timedelta(hours=2)).timestamp(), read_from=TEMP
    )
    await async_wait_recording_done(hass)

    assert hours == 2
    assert await read_sums(hass, COUNT_OFF, T0, T0 + timedelta(hours=2)) == [1.0, 1.0]


async def test_earliest_recorded_ts_never_consults_the_state_machine(recorder_utc):
    hass = recorder_utc
    compiler = compiler_module.Compiler(hass)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    assert await compiler.async_earliest_recorded_ts(ENTITY) is not None
    assert await compiler.async_earliest_recorded_ts(TEMP) is None
    # The state machine alone still answers `async_earliest_state_ts`.
    with patch.object(compiler, "async_earliest_recorded_ts", return_value=None):
        assert await compiler.async_earliest_state_ts(ENTITY) is not None


async def _seed(hass, statistic_id, start, sums):
    async_add_external_statistics(
        hass,
        metadata_for(METRIC_DURATION, statistic_id, "Grid Status: On (h)"),
        [{"start": start + timedelta(hours=i), "sum": s} for i, s in enumerate(sums)],
    )
    await async_wait_recording_done(hass)


async def test_rename_moves_every_statistic_of_the_entity(recorder_utc):
    hass = recorder_utc
    await _seed(hass, DURATION_ON, T0, [0.5, 1.0])
    await _seed(hass, DURATION_OFF, T0, [0.5, 1.0])
    await _seed(
        hass, "discrete_statistics:binary_sensor_grid_status_2_on_duration", T0, [1.0]
    )

    result = await compiler_module.Compiler(hass).async_rename(ENTITY, NEW)

    assert sorted(result.moved) == sorted([DURATION_ON, DURATION_OFF])
    assert result.collided == []
    assert await existing(hass) == []
    assert await existing(hass, NEW) == sorted([NEW_ON, NEW_OFF])
    # A longer slug sharing the prefix is another entity's and stays.
    assert await existing(hass, TEMP) == [
        "discrete_statistics:binary_sensor_grid_status_2_on_duration"
    ]
    # The rows came with the metadata.
    assert await read_sums(
        hass, NEW_ON, T0, T0 + timedelta(hours=2), entity_id=NEW
    ) == [0.5, 1.0]


async def test_rename_leaves_a_colliding_statistic_in_place(recorder_utc, caplog):
    hass = recorder_utc
    await _seed(hass, DURATION_ON, T0, [0.5])
    await _seed(hass, DURATION_OFF, T0, [0.5])
    await _seed(hass, NEW_ON, T0, [9.0])

    result = await compiler_module.Compiler(hass).async_rename(ENTITY, NEW)

    assert result.moved == [DURATION_OFF]
    assert result.collided == [DURATION_ON]
    assert await existing(hass) == [DURATION_ON]
    assert await existing(hass, NEW) == sorted([NEW_ON, NEW_OFF])
    assert await read_sums(
        hass, NEW_ON, T0, T0 + timedelta(hours=1), entity_id=NEW
    ) == [9.0]
    # Checked before core is asked, so core's own error is never logged.
    assert "Cannot rename statistic_id" not in caplog.text


async def test_rename_returns_only_after_the_commit(recorder_utc, monkeypatch):
    """What the caller does next reads the metadata, so it must be there."""
    hass = recorder_utc
    await _seed(hass, DURATION_ON, T0, [0.5])
    order: list[str] = []
    real_run = compiler_module.RenameTask.run

    def run(self, instance):
        real_run(self, instance)
        order.append("committed")

    monkeypatch.setattr(compiler_module.RenameTask, "run", run)
    await compiler_module.Compiler(hass).async_rename(ENTITY, NEW)
    order.append("returned")
    assert order == ["committed", "returned"]
    assert await existing(hass, NEW) == [NEW_ON]


async def test_rename_of_an_entity_with_no_statistics_is_a_no_op(recorder_utc):
    result = await compiler_module.Compiler(recorder_utc).async_rename(ENTITY, NEW)
    assert result == compiler_module.Renamed([], [])


async def test_fill_compiles_the_source_history_into_our_series(recorder_utc, freezer):
    """From the last count row to the hour before the rename, under our IDs."""
    hass = recorder_utc
    compiler = compiler_module.Compiler(hass)
    ours = cfg()
    # Ours: on at T0, off at T0+1h (the last transition), then the old
    # device dies - removed at T0+2h, so hour2 carries `off` forward.
    await play(hass, freezer, [(T0, "on"), (T0 + timedelta(hours=1), "off")])
    freezer.move_to(T0 + timedelta(hours=2))
    hass.states.async_remove(ENTITY)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    freezer.move_to(T0 + timedelta(hours=3))
    await compiler.async_compile(ours, T0.timestamp())
    await async_wait_recording_done(hass)
    # The replacement under its temporary ID: on from T0+3h, off at T0+5h.
    await play(
        hass,
        freezer,
        [(T0 + timedelta(hours=3), "on"), (T0 + timedelta(hours=5), "off")],
        entity_id=TEMP,
    )
    renamed_at = T0 + timedelta(hours=6, minutes=20)
    freezer.move_to(renamed_at)

    hours = await compiler.async_fill(ours, TEMP, renamed_at.timestamp())
    await async_wait_recording_done(hass)

    # T0+3h .. T0+6h: three hours of the replacement's states, and nothing
    # under its own ID.
    assert hours == 3
    assert await existing(hass, TEMP) == []
    # hours 0-5: on, off, off (dead, carried), on, on, off
    assert await read_sums(hass, DURATION_ON, T0, T0 + timedelta(hours=6)) == [
        1.0,
        1.0,
        1.0,
        2.0,
        3.0,
        3.0,
    ]
    assert await read_sums(hass, DURATION_OFF, T0, T0 + timedelta(hours=6)) == [
        0.0,
        1.0,
        2.0,
        2.0,
        2.0,
        3.0,
    ]


async def test_fill_reads_under_our_own_id_when_the_history_moved(
    recorder_utc, freezer
):
    """No states_meta row of ours means the recorder's rename succeeded."""
    hass = recorder_utc
    compiler = compiler_module.Compiler(hass)
    ours = cfg()
    # A duration statistic with no state history and no count statistic
    # behind it, seeded at a base sum of 1.0 hour before the window.
    await _seed(hass, DURATION_ON, T0 - timedelta(hours=2), [0.5, 1.0])
    # The replacement's history is already under our ID, as the recorder
    # leaves it after a rename that met no collision.
    await play(hass, freezer, [(T0, "on"), (T0 + timedelta(hours=1), "off")])
    renamed_at = T0 + timedelta(hours=2, minutes=5)
    freezer.move_to(renamed_at)

    hours = await compiler.async_fill(ours, TEMP, renamed_at.timestamp())
    await async_wait_recording_done(hass)

    assert hours == 2
    # hour0: on, adding an hour on top of the seeded 1.0; hour1: off, carried.
    assert await read_sums(hass, DURATION_ON, T0, T0 + timedelta(hours=2)) == [2.0, 2.0]


async def test_fill_with_nothing_to_read_compiles_nothing(recorder_utc, freezer):
    hass = recorder_utc
    freezer.move_to(T0)
    ours = cfg()
    assert (
        await compiler_module.Compiler(hass).async_fill(ours, TEMP, T0.timestamp()) == 0
    )


async def test_fill_uses_the_count_watermark_not_the_duration_one(
    recorder_utc, freezer
):
    """A dead entity's carried duration rows must not shadow the last transition.

    The old device stops transitioning at T0+1h but is compiled on through
    T0+6h, carrying `off` the whole way - so its duration rows reach
    further than its last real change. The replacement's history begins
    at T0+3h, inside that carried span. Watermarking on the count
    statistics (the last real transition) rather than on all of them is
    what lets the fill reach back far enough to read it.
    """
    hass = recorder_utc
    compiler = compiler_module.Compiler(hass)
    ours = cfg()
    # Ours: on at T0, off at T0+1h - its last real transition, then it
    # just carries `off`, never removed.
    await play(hass, freezer, [(T0, "on"), (T0 + timedelta(hours=1), "off")])
    # The replacement's history, recorded before ours is compiled past it.
    await play(
        hass,
        freezer,
        [(T0 + timedelta(hours=3), "on"), (T0 + timedelta(hours=5), "off")],
        entity_id=TEMP,
    )
    freezer.move_to(T0 + timedelta(hours=7))
    await compiler.async_compile(
        ours, T0.timestamp(), (T0 + timedelta(hours=6)).timestamp()
    )
    await async_wait_recording_done(hass)

    renamed_at = T0 + timedelta(hours=6, minutes=20)
    freezer.move_to(renamed_at)
    hours = await compiler.async_fill(ours, TEMP, renamed_at.timestamp())
    await async_wait_recording_done(hass)

    assert hours == 3
    # hours 0-5: on, off, off (carried), on, on, off - the replacement's
    # on/on/off reaching all the way back to T0+3h, not just its last hour.
    assert await read_sums(hass, DURATION_ON, T0, T0 + timedelta(hours=6)) == [
        1.0,
        1.0,
        1.0,
        2.0,
        3.0,
        3.0,
    ]
    assert await read_sums(hass, DURATION_OFF, T0, T0 + timedelta(hours=6)) == [
        0.0,
        1.0,
        2.0,
        2.0,
        2.0,
        3.0,
    ]


async def test_fill_returns_zero_when_the_rename_lands_within_the_last_compiled_hour(
    recorder_utc, freezer
):
    """No compile at all, not one that happens to compile nothing.

    `async_compile` would itself return 0 on an empty, hour-aligned window
    - both `start` and `end` already are - so asserting on `hours` alone
    cannot tell a real guard from none at all. Asserting the call never
    happens is what pins the guard.
    """
    hass = recorder_utc
    compiler = compiler_module.Compiler(hass)
    ours = cfg()
    await play(hass, freezer, [(T0, "on"), (T0 + timedelta(hours=1), "off")])
    freezer.move_to(T0 + timedelta(hours=2))
    await compiler.async_compile(ours, T0.timestamp())
    await async_wait_recording_done(hass)
    # The replacement's own history, elsewhere - not what this test probes.
    await play(hass, freezer, [(T0 + timedelta(hours=2), "on")], entity_id=TEMP)
    # Same hour as the last count row (T0+1h): the window is empty.
    renamed_at = T0 + timedelta(hours=1, minutes=30)
    freezer.move_to(renamed_at)

    with patch.object(
        compiler, "async_compile", wraps=compiler.async_compile
    ) as async_compile:
        hours = await compiler.async_fill(ours, TEMP, renamed_at.timestamp())

    assert hours == 0
    async_compile.assert_not_called()
