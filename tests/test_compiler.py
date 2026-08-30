"""Tests for the compiler against a real recorder."""

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.setup import async_setup_component

from homeassistant.components.recorder.statistics import async_add_external_statistics

from custom_components.discrete_stats.compiler import TRAILING_HOURS, Compiler
from custom_components.discrete_stats.config import EntityConfig
from custom_components.discrete_stats.const import HOUR, METRIC_COUNT, METRIC_SECONDS
from custom_components.discrete_stats.payload import metadata_for
from custom_components.discrete_stats.registry import Registry

ENTITY = "binary_sensor.grid_status"
SECONDS_OFF = "discrete_stats:binary_sensor_grid_status_off_seconds"
COUNT_OFF = "discrete_stats:binary_sensor_grid_status_off_count"
SECONDS_ON = "discrete_stats:binary_sensor_grid_status_on_seconds"


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
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    assert await compiler.async_compile_incremental(cfg()) == 0


async def test_registers_statistic_ids_it_writes(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    assert SECONDS_ON in registry.statistic_ids_for(ENTITY)
    assert registry.describe(SECONDS_ON) == (ENTITY, "on", "seconds")


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
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    sums = await read_sums(
        hass, SECONDS_OFF, start, start + timedelta(hours=2)
    )
    assert sums[0] == pytest.approx(900.0)
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
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    await compiler.async_compile(cfg(), start.timestamp())
    first = await read_sums(hass, SECONDS_OFF, start, start + timedelta(hours=2))
    await compiler.async_compile(cfg(), start.timestamp())
    second = await read_sums(hass, SECONDS_OFF, start, start + timedelta(hours=2))

    # "off" runs from 00:30 to the end of the window: 1800 s in the first
    # hour, a full hour in the second. Assert the values, not just that two
    # reads agree - they would agree if both were empty.
    assert first == [1800.0, 5400.0]
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

    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    freezer.move_to(start + timedelta(hours=5))
    await compiler.async_compile(cfg(), start.timestamp())
    all_at_once = await read_sums(
        hass, SECONDS_OFF, start, start + timedelta(hours=5)
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
        hass, SECONDS_OFF, start, start + timedelta(hours=5)
    )

    assert all_at_once == stepwise


async def test_a_state_absent_from_a_window_keeps_its_cumulative_base(
    recorder, freezer
):
    """A statistic missing from a window must not restart its sum at zero."""
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
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    for window in range(3):
        window_start = start + timedelta(hours=2 * window)
        await compiler.async_compile(
            cfg(),
            window_start.timestamp(),
            (window_start + timedelta(hours=2)).timestamp(),
        )

    sums = await read_sums(hass, SECONDS_OFF, start, start + timedelta(hours=6))

    assert len(sums) == 6
    # 1800 s in the first window, nothing in the second, 900 s in the third.
    assert sums == sorted(sums), f"cumulative sum went backwards: {sums}"
    assert sums[-1] == pytest.approx(2700.0)


async def test_back_to_back_compiles_see_the_previous_write(recorder, freezer):
    """Adjacent windows compiled in succession must carry the base forward.

    Deliberately without draining the recorder between the two calls: the
    compiler drains its own writes before returning, so a caller compiling
    one window after another does not have to know about the queue.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    for offset, state in (
        (timedelta(minutes=30), "off"),
        (timedelta(hours=1), "on"),
        (timedelta(hours=2, minutes=30), "off"),
        (timedelta(hours=2, minutes=45), "on"),
        (timedelta(hours=4, minutes=15), "off"),
        (timedelta(hours=4, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    for window in range(3):
        window_start = start + timedelta(hours=2 * window)
        await compiler.async_compile(
            cfg(),
            window_start.timestamp(),
            (window_start + timedelta(hours=2)).timestamp(),
        )

    sums = await read_sums(hass, SECONDS_OFF, start, start + timedelta(hours=6))

    # 1800 s of "off" in the first window, then 900 s in each of the next two.
    assert sums == sorted(sums), f"cumulative sum went backwards: {sums}"
    assert sums == [1800.0, 1800.0, 2700.0, 2700.0, 3600.0, 3600.0]


async def test_watermark_is_the_newest_hour_across_statistics(recorder):
    """The watermark must not follow the alphabetically first statistic."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    registry = Registry(hass)
    await registry.async_load()
    # COUNT_OFF sorts before SECONDS_ON, and is the one left behind.
    await registry.async_register(
        ENTITY,
        {COUNT_OFF: ("off", METRIC_COUNT), SECONDS_ON: ("on", METRIC_SECONDS)},
    )
    assert registry.statistic_ids_for(ENTITY)[0] == COUNT_OFF

    for statistic_id, state, metric, hours in (
        (COUNT_OFF, "off", METRIC_COUNT, 2),
        (SECONDS_ON, "on", METRIC_SECONDS, 4),
    ):
        async_add_external_statistics(
            hass,
            metadata_for(cfg(), state, metric, statistic_id),
            [
                {"start": start + timedelta(hours=hour), "sum": float(hour)}
                for hour in range(hours)
            ],
        )
    await get_instance(hass).async_block_till_done()

    compiler = Compiler(hass, registry)
    watermark = await compiler._async_watermark(ENTITY)

    assert watermark == (start + timedelta(hours=3)).timestamp()


async def test_missing_history_before_first_state_is_no_data(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    no_data = "discrete_stats:binary_sensor_grid_status_no_data_seconds"
    sums = await read_sums(hass, no_data, start, start + timedelta(hours=4))
    assert sums[-1] == pytest.approx(2 * HOUR)
