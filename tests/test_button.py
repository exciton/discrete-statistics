"""Tests for the recompute button."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_ENTITY_ID,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discrete_statistics.config import CONF_BLANK, CONF_DEFAULT
from custom_components.discrete_statistics.const import DEFAULT_RECORD_KNOWN, DOMAIN

ENTITY = "binary_sensor.grid_status"
BUTTON = "button.grid_status_statistics"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override the root conftest fixture; recorder fixtures must come first."""
    yield


@pytest.fixture
async def recorder(recorder_mock, hass):
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


async def _entry(hass, name="Grid Status"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: ENTITY},
        options={
            CONF_NAME: name,
            CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
            CONF_BLANK: STATE_UNKNOWN,
        },
        unique_id=ENTITY,
        title=name,
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


async def test_an_entry_gets_a_button(recorder):
    """Which is also what stops the Helpers list flagging it as entity-less."""
    hass = recorder
    entry = await _entry(hass)

    entities = er.async_get(hass).entities.get_entries_for_config_entry_id(
        entry.entry_id
    )
    assert [e.domain for e in entities] == [Platform.BUTTON]
    assert entities[0].unique_id == f"{entry.entry_id}_recompute"
    assert hass.states.get(BUTTON) is not None


async def test_pressing_it_recompiles_the_whole_history(recorder):
    """Full, not incremental: the point is to rebuild what looks wrong."""
    hass = recorder
    await _entry(hass)

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile",
        return_value=0,
    ) as full, patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ) as incremental:
        await hass.services.async_call(
            "button", "press", {"entity_id": BUTTON}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert full.called
    assert not incremental.called
    # `start=None` is what makes it the entity's whole retained history.
    assert full.call_args.args[1] is None


async def test_pressing_it_notifies(recorder):
    hass = recorder
    entry = await _entry(hass)

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile",
        return_value=7,
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": BUTTON}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    notifications = hass.data.get("persistent_notification", {})
    assert f"{DOMAIN}_{entry.entry_id}" in notifications


async def test_unloading_the_entry_takes_the_button_down(recorder):
    """The platform must unload with the entry, or a reload would fail.

    The registry keeps the entity, so Home Assistant leaves a restored
    placeholder behind rather than removing the state - what matters is that
    it is no longer live.
    """
    hass = recorder
    entry = await _entry(hass)
    assert hass.states.get(BUTTON).state != STATE_UNAVAILABLE

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(BUTTON).state == STATE_UNAVAILABLE


async def test_the_entry_can_be_reloaded(recorder):
    """Which is what an unforwarded platform would break."""
    hass = recorder
    entry = await _entry(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(BUTTON).state != STATE_UNAVAILABLE
