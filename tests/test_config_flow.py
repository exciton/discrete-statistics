"""Tests for the config flow."""

from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import CoreState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discrete_statistics.config import CONF_DEFAULT
from custom_components.discrete_statistics.const import (
    DEFAULT_IGNORE,
    DEFAULT_RECORD,
    DEFAULT_RECORD_KNOWN,
    DOMAIN,
)

ENTITY = "binary_sensor.grid_status"


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


async def test_user_flow_creates_an_entry(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_ENTITY_ID: ENTITY,
                CONF_NAME: "Grid Status",
                CONF_DEFAULT: DEFAULT_RECORD,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Grid Status"
    assert result["data"] == {CONF_ENTITY_ID: ENTITY}
    assert result["options"] == {CONF_NAME: "Grid Status", CONF_DEFAULT: DEFAULT_RECORD}


async def test_flow_rejects_an_entity_already_in_an_entry(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTITY_ID: ENTITY}, unique_id=ENTITY
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENTITY_ID: ENTITY, CONF_DEFAULT: DEFAULT_RECORD_KNOWN},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_flow_rejects_an_entity_configured_in_yaml(recorder):
    hass = recorder
    assert await async_setup_component(
        hass, DOMAIN, {DOMAIN: [{"entity_id": ENTITY, "name": "From YAML"}]}
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENTITY_ID: ENTITY, CONF_DEFAULT: DEFAULT_RECORD_KNOWN},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "yaml_configured"


async def test_flow_does_not_offer_ignore(recorder):
    # `ignore` with no per-state mapping makes every state resolve to None,
    # so the entity's whole timeline becomes no_data. It returns to the
    # dropdown with the state-mapping screen. YAML still accepts it.
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with pytest.raises(vol.Invalid):
        result["data_schema"](
            {CONF_ENTITY_ID: ENTITY, CONF_DEFAULT: DEFAULT_IGNORE}
        )


async def test_options_flow_updates_and_recompiles(recorder):
    hass = recorder
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: ENTITY},
        options={CONF_NAME: "Grid Status", CONF_DEFAULT: DEFAULT_RECORD_KNOWN},
        unique_id=ENTITY,
        title="Grid Status",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile",
        return_value=48,
    ) as full_mock:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "init"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_NAME: "Grid", CONF_DEFAULT: DEFAULT_RECORD},
        )
        await hass.async_block_till_done()

    assert entry.options == {CONF_NAME: "Grid", CONF_DEFAULT: DEFAULT_RECORD}
    # A disposition change reattributes every past hour, so the recompute
    # must start from the earliest retained state, not from the watermark.
    assert full_mock.called
    assert full_mock.call_args.args[1] is None
    # The stored config reflects the new options.
    cfg = hass.data[DOMAIN]["entry_configs"][entry.entry_id]
    assert cfg.default == DEFAULT_RECORD
    assert cfg.name == "Grid"
