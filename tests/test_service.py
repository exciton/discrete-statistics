"""Tests for the recompute service."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.setup import async_setup_component

from homeassistant.exceptions import ServiceValidationError

from custom_components.discrete_statistics.const import DOMAIN

ENTITY = "binary_sensor.grid_status"
SECONDS_OFF = "discrete_statistics:binary_sensor_grid_status_off_seconds"

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
async def recorder(hass, recorder_mock):
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


async def read_sums(hass, statistic_id, start, end):
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


async def test_service_is_registered(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "recompute")


async def test_backfill_writes_history(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    await hass.services.async_call(
        DOMAIN, "recompute", {"entity_id": ENTITY}, blocking=True
    )
    await hass.async_block_till_done()

    sums = await read_sums(hass, SECONDS_OFF, start, start + timedelta(hours=2))
    assert sums
    assert sums[-1] > 0


async def test_clear_removes_registered_ids(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    await hass.services.async_call(
        DOMAIN, "recompute", {"entity_id": ENTITY}, blocking=True
    )
    await hass.async_block_till_done()
    registry = hass.data[DOMAIN]["registry"]
    assert registry.statistic_ids_for(ENTITY)

    await hass.services.async_call(
        DOMAIN,
        "recompute",
        {"entity_id": ENTITY, "clear": True},
        blocking=True,
    )
    await hass.async_block_till_done()
    # Cleared, then immediately recompiled, so IDs are present again.
    assert registry.statistic_ids_for(ENTITY)


async def test_service_serialises_with_scheduled_runs(recorder, freezer):
    """The service and the hourly run must not compile concurrently."""
    hass = recorder
    freezer.move_to(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    concurrent = 0
    peak = 0

    async def fake(self, cfg, *args, **kwargs):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0)
        concurrent -= 1
        return 0

    with (
        patch("custom_components.discrete_statistics.Compiler.async_compile", fake),
        patch(
            "custom_components.discrete_statistics.Compiler.async_compile_incremental",
            fake,
        ),
    ):
        await asyncio.gather(
            hass.services.async_call(
                DOMAIN, "recompute", {"entity_id": ENTITY}, blocking=True
            ),
            hass.data[DOMAIN]["compile_all"](),
        )

    assert peak == 1


async def test_recompute_logs_what_it_did(recorder, freezer, caplog):
    """A hand-invoked service must be confirmable without debug logging."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="custom_components.discrete_statistics"):
        await hass.services.async_call(
            DOMAIN, "recompute", {"entity_id": ENTITY}, blocking=True
        )
        await hass.async_block_till_done()

    assert "Recompute: compiled" in caplog.text
    assert ENTITY in caplog.text
    assert "the earliest retained state" in caplog.text


async def test_recompute_logs_the_explicit_start(recorder, freezer, caplog):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="custom_components.discrete_statistics"):
        await hass.services.async_call(
            DOMAIN,
            "recompute",
            {"entity_id": ENTITY, "start": "2026-01-01T01:00:00+00:00"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert "Recompute: compiled" in caplog.text
    assert "2026-01-01T01:00:00" in caplog.text


async def test_unconfigured_entity_is_rejected(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="not configured"):
        await hass.services.async_call(
            DOMAIN,
            "recompute",
            {"entity_id": "binary_sensor.not_configured"},
            blocking=True,
        )


async def test_clear_with_start_is_rejected(recorder, freezer):
    """clear deletes every hour, so a partial rebuild would destroy history."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await hass.services.async_call(
        DOMAIN, "recompute", {"entity_id": ENTITY}, blocking=True
    )
    await hass.async_block_till_done()
    before = await read_sums(hass, SECONDS_OFF, start, start + timedelta(hours=4))
    assert len(before) == 4, before

    with pytest.raises(ServiceValidationError, match="cannot be combined"):
        await hass.services.async_call(
            DOMAIN,
            "recompute",
            {
                "entity_id": ENTITY,
                "clear": True,
                "start": start + timedelta(hours=2),
            },
            blocking=True,
        )
    await hass.async_block_till_done()

    # Nothing was deleted: the hours before `start` are still there.
    after = await read_sums(hass, SECONDS_OFF, start, start + timedelta(hours=4))
    assert after == before
    assert hass.data[DOMAIN]["registry"].statistic_ids_for(ENTITY)


async def test_clear_without_start_is_still_allowed(recorder, freezer):
    """The rejection must not break the legitimate full-rebuild workflow."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    await hass.services.async_call(
        DOMAIN, "recompute", {"entity_id": ENTITY, "clear": True}, blocking=True
    )
    await hass.async_block_till_done()

    sums = await read_sums(hass, SECONDS_OFF, start, start + timedelta(hours=4))
    assert sums == [1800.0, 5400.0, 9000.0, 12600.0]
