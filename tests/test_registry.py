"""Tests for the statistic registry."""

import pytest

from custom_components.discrete_statistics.registry import Registry

ENTITY = "binary_sensor.grid_status"
DURATION_ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
COUNT_ON = "discrete_statistics:binary_sensor_grid_status_on_count"


async def test_starts_empty(hass):
    registry = Registry(hass)
    await registry.async_load()
    assert registry.statistic_ids_for(ENTITY) == []
    assert registry.describe(DURATION_ON) is None


async def test_register_and_describe(hass):
    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(
        ENTITY, {DURATION_ON: ("on", "duration"), COUNT_ON: ("on", "count")}
    )
    assert registry.statistic_ids_for(ENTITY) == sorted([DURATION_ON, COUNT_ON])
    assert registry.describe(DURATION_ON) == (ENTITY, "on", "duration")


async def test_register_is_idempotent(hass):
    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(ENTITY, {DURATION_ON: ("on", "duration")})
    await registry.async_register(ENTITY, {DURATION_ON: ("on", "duration")})
    assert registry.statistic_ids_for(ENTITY) == [DURATION_ON]


async def test_survives_a_reload(hass):
    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(ENTITY, {DURATION_ON: ("on", "duration")})

    reloaded = Registry(hass)
    await reloaded.async_load()
    assert reloaded.describe(DURATION_ON) == (ENTITY, "on", "duration")


async def test_forget_drops_an_entry(hass):
    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(
        ENTITY, {DURATION_ON: ("on", "duration"), COUNT_ON: ("on", "count")}
    )

    await registry.async_forget({DURATION_ON})

    assert registry.statistic_ids_for(ENTITY) == [COUNT_ON]
    assert registry.describe(DURATION_ON) is None

    reloaded = Registry(hass)
    await reloaded.async_load()
    assert reloaded.statistic_ids_for(ENTITY) == [COUNT_ON]


async def test_forget_ignores_ids_it_does_not_hold(hass):
    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(ENTITY, {DURATION_ON: ("on", "duration")})

    await registry.async_forget({COUNT_ON})
    await registry.async_forget(set())

    assert registry.statistic_ids_for(ENTITY) == [DURATION_ON]
