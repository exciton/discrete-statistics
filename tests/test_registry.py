"""Tests for the statistic registry."""

import pytest

from custom_components.discrete_stats.registry import Registry

ENTITY = "binary_sensor.grid_status"
SECONDS_ON = "discrete_stats:binary_sensor_grid_status_on_seconds"
COUNT_ON = "discrete_stats:binary_sensor_grid_status_on_count"


async def test_starts_empty(hass):
    registry = Registry(hass)
    await registry.async_load()
    assert registry.statistic_ids_for(ENTITY) == []
    assert registry.describe(SECONDS_ON) is None


async def test_register_and_describe(hass):
    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(
        ENTITY, {SECONDS_ON: ("on", "seconds"), COUNT_ON: ("on", "count")}
    )
    assert registry.statistic_ids_for(ENTITY) == sorted([SECONDS_ON, COUNT_ON])
    assert registry.describe(SECONDS_ON) == (ENTITY, "on", "seconds")


async def test_register_is_idempotent(hass):
    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(ENTITY, {SECONDS_ON: ("on", "seconds")})
    await registry.async_register(ENTITY, {SECONDS_ON: ("on", "seconds")})
    assert registry.statistic_ids_for(ENTITY) == [SECONDS_ON]


async def test_survives_a_reload(hass):
    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(ENTITY, {SECONDS_ON: ("on", "seconds")})

    reloaded = Registry(hass)
    await reloaded.async_load()
    assert reloaded.describe(SECONDS_ON) == (ENTITY, "on", "seconds")


async def test_forget_returns_and_removes_ids(hass):
    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(
        ENTITY, {SECONDS_ON: ("on", "seconds"), COUNT_ON: ("on", "count")}
    )
    removed = await registry.async_forget(ENTITY)
    assert sorted(removed) == sorted([SECONDS_ON, COUNT_ON])
    assert registry.statistic_ids_for(ENTITY) == []
    assert registry.describe(SECONDS_ON) is None
