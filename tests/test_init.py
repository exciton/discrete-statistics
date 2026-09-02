"""Tests for setup and scheduling."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import DEFAULT_RECORD_KNOWN, DOMAIN

ENTITY = "binary_sensor.grid_status"
OTHER_ENTITY = "binary_sensor.water_pump"

CONFIG = {DOMAIN: [{"entity_id": ENTITY, "name": "Grid Status"}]}


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
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


async def test_setup_stores_runtime_data(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data
    assert len(hass.data[DOMAIN]["yaml_configs"]) == 1
    assert hass.data[DOMAIN]["yaml_configs"][0].entity_id == ENTITY


async def test_all_configs_joins_yaml_and_entries(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()
    data = hass.data[DOMAIN]

    assert [c.entity_id for c in data["all_configs"]()] == [ENTITY]

    data["entry_configs"]["entry-1"] = EntityConfig(
        entity_id=OTHER_ENTITY, name=None, default=DEFAULT_RECORD_KNOWN
    )
    assert [c.entity_id for c in data["all_configs"]()] == [ENTITY, OTHER_ENTITY]


async def test_hourly_schedule_triggers_a_compile(recorder, freezer):
    hass = recorder
    freezer.move_to(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ) as compile_mock:
        freezer.move_to(datetime(2026, 1, 1, 11, 3, 0, tzinfo=timezone.utc))
        async_fire_time_changed(hass, datetime(2026, 1, 1, 11, 3, 0, tzinfo=timezone.utc))
        await hass.async_block_till_done()

    assert compile_mock.called


async def test_backlog_gate_skips_the_run(recorder, freezer):
    hass = recorder
    freezer.move_to(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    with (
        patch(
            "custom_components.discrete_statistics.Compiler.async_compile_incremental",
            return_value=0,
        ) as compile_mock,
        patch(
            "custom_components.discrete_statistics.get_instance",
            return_value=Mock(backlog=10_000),
        ),
    ):
        when = datetime(2026, 1, 1, 11, 3, 0, tzinfo=timezone.utc)
        freezer.move_to(when)
        async_fire_time_changed(hass, when)
        await hass.async_block_till_done()

    assert not compile_mock.called


async def test_runs_do_not_overlap(recorder, freezer):
    """Two concurrent runs must serialise; overlapping compiles corrupt sums."""
    hass = recorder
    freezer.move_to(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    concurrent = 0
    peak = 0

    async def fake_compile(self, cfg):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0)
        concurrent -= 1
        return 0

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        fake_compile,
    ):
        compile_all = hass.data[DOMAIN]["compile_all"]
        await asyncio.gather(compile_all(), compile_all())

    assert peak == 1
