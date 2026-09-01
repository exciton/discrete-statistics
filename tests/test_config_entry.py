"""Tests for the config entry lifecycle."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import CoreState
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.discrete_statistics.config import CONF_DEFAULT
from custom_components.discrete_statistics.const import DEFAULT_RECORD_KNOWN, DOMAIN

ENTITY = "binary_sensor.grid_status"
DURATION_ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
DURATION_OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"


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


def make_entry(entity_id: str = ENTITY) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: entity_id},
        options={CONF_NAME: "Grid Status", CONF_DEFAULT: DEFAULT_RECORD_KNOWN},
        unique_id=entity_id,
        title="Grid Status",
    )


async def test_entry_registers_its_config(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    entry = make_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert [c.entity_id for c in hass.data[DOMAIN]["all_configs"]()] == [ENTITY]


async def test_unload_removes_the_config(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    entry = make_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.data[DOMAIN]["all_configs"]() == []


async def test_entry_compiles_when_hass_is_running(recorder):
    hass = recorder
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, DOMAIN, {})
    entry = make_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=3,
    ) as compile_mock:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert compile_mock.called


async def test_entry_does_not_compile_during_startup(recorder):
    # At boot the EVENT_HOMEASSISTANT_STARTED handler compiles every config,
    # so compiling here as well would do each entity twice.
    hass = recorder
    hass.set_state(CoreState.starting)
    assert await async_setup_component(hass, DOMAIN, {})
    entry = make_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=3,
    ) as compile_mock:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert not compile_mock.called


async def test_hourly_run_and_service_see_entry_configs(recorder):
    # all_configs() is only useful if its two consumers actually read it.
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    entry = make_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=1,
    ) as hourly_mock:
        await hass.data[DOMAIN]["compile_all"]()
    assert [call.args[0].entity_id for call in hourly_mock.call_args_list] == [ENTITY]

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile",
        return_value=1,
    ) as service_mock:
        await hass.services.async_call(DOMAIN, "recompute", {}, blocking=True)
    assert [call.args[0].entity_id for call in service_mock.call_args_list] == [ENTITY]


async def test_entry_clashing_with_yaml_fails_and_is_excluded(recorder):
    hass = recorder
    yaml_config = {DOMAIN: [{"entity_id": ENTITY, "name": "From YAML"}]}
    assert await async_setup_component(hass, DOMAIN, yaml_config)
    await hass.async_block_till_done()

    entry = make_entry()
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    # The entity is configured exactly once, by YAML.
    assert [c.entity_id for c in hass.data[DOMAIN]["all_configs"]()] == [ENTITY]
    assert hass.data[DOMAIN]["entry_configs"] == {}


async def test_compile_failure_notifies_instead_of_raising(recorder):
    hass = recorder
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, DOMAIN, {})
    entry = make_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        side_effect=RuntimeError("recorder exploded"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    notifications = hass.data.get("persistent_notification", {})
    assert any("recorder exploded" in n["message"] for n in notifications.values())


async def test_removing_a_clashing_entry_clears_its_issue(recorder):
    # A user who resolves a YAML clash the obvious way - deleting the helper
    # - must not be left with a permanent repair card naming an entry that
    # no longer exists. async_unload_entry never runs for a SETUP_ERROR
    # entry, so the issue has to be cleared from async_remove_entry instead.
    hass = recorder
    yaml_config = {DOMAIN: [{"entity_id": ENTITY, "name": "From YAML"}]}
    assert await async_setup_component(hass, DOMAIN, yaml_config)
    await hass.async_block_till_done()

    entry = make_entry()
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"yaml_clash_{entry.entry_id}") is not None

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get_issue(DOMAIN, f"yaml_clash_{entry.entry_id}") is None


async def test_entry_backfills_history_end_to_end(recorder, freezer):
    """A helper created over existing history compiles all of it.

    Seeded history must run comfortably longer than TRAILING_HOURS (3): the
    no-watermark branch of async_compile_incremental is supposed to start
    from the entity's earliest state, not from a trailing window. At exactly
    TRAILING_HOURS of history the two are indistinguishable and this test
    would pass even if that branch regressed to trailing-window arithmetic -
    do not shrink this back down to 3.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

    # Seed five hours of history: on for three hours, then off for two.
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=3))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    freezer.move_to(start + timedelta(hours=5))
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, DOMAIN, {})
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    # The compile runs as a background task (see _async_compile_and_notify);
    # plain async_block_till_done does not wait for those.
    await hass.async_block_till_done(wait_background_tasks=True)

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        start + timedelta(hours=5),
        {DURATION_ON, DURATION_OFF},
        "hour",
        None,
        {"sum"},
    )
    # Durations are cumulative sums in hours; the last sum of each state
    # over five hours must total exactly five.
    total = sum(rows[-1]["sum"] for rows in stats.values())
    assert total == pytest.approx(5.0)
