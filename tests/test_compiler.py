"""Tests for the compiler against a real recorder."""

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.setup import async_setup_component

from custom_components.discrete_stats.compiler import Compiler
from custom_components.discrete_stats.config import EntityConfig
from custom_components.discrete_stats.const import HOUR
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

    assert first == second


async def test_cadence_invariance(recorder, freezer):
    """Compiling hour-by-hour must equal compiling all at once."""
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

    # Recompute the same range in one-hour steps; results must be identical.
    for hour in range(1, 6):
        await compiler.async_compile(
            cfg(),
            start.timestamp(),
            (start + timedelta(hours=hour)).timestamp(),
        )
    stepwise = await read_sums(
        hass, SECONDS_OFF, start, start + timedelta(hours=5)
    )

    assert all_at_once == stepwise


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
