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

from homeassistant.const import STATE_UNKNOWN

from custom_components.discrete_statistics.config import CONF_BLANK, CONF_DEFAULT
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
                CONF_BLANK: STATE_UNKNOWN,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Grid Status"
    assert result["data"] == {CONF_ENTITY_ID: ENTITY}
    assert result["options"] == {
        CONF_NAME: "Grid Status",
        CONF_DEFAULT: DEFAULT_RECORD,
        CONF_BLANK: STATE_UNKNOWN,
    }


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
        {CONF_ENTITY_ID: ENTITY, CONF_DEFAULT: DEFAULT_RECORD_KNOWN, CONF_BLANK: STATE_UNKNOWN},
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
        {CONF_ENTITY_ID: ENTITY, CONF_DEFAULT: DEFAULT_RECORD_KNOWN, CONF_BLANK: STATE_UNKNOWN},
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
            {CONF_ENTITY_ID: ENTITY, CONF_DEFAULT: DEFAULT_IGNORE, CONF_BLANK: STATE_UNKNOWN}
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
            {CONF_NAME: "Grid", CONF_DEFAULT: DEFAULT_RECORD, CONF_BLANK: STATE_UNKNOWN},
        )
        await hass.async_block_till_done()

    assert entry.options == {
        CONF_NAME: "Grid",
        CONF_DEFAULT: DEFAULT_RECORD,
        CONF_BLANK: STATE_UNKNOWN,
    }
    # A disposition change reattributes every past hour, so the recompute
    # must start from the earliest retained state, not from the watermark.
    assert full_mock.called
    assert full_mock.call_args.args[1] is None
    # The stored config reflects the new options.
    cfg = hass.data[DOMAIN]["entry_configs"][entry.entry_id]
    assert cfg.default == DEFAULT_RECORD
    assert cfg.name == "Grid"


async def test_options_flow_name_only_change_skips_recompile(recorder):
    # A name-only edit changes only how a series is displayed - payload
    # rebuilds statistic metadata on every ordinary compile, so it needs no
    # history rewrite. A full recompute here would take the shared lock for
    # no reason and raise a notification the user did not ask for.
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
    ) as full_mock:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Grid",
                CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
                CONF_BLANK: STATE_UNKNOWN,
            },
        )
        await hass.async_block_till_done()

    assert entry.options == {
        CONF_NAME: "Grid",
        CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
        CONF_BLANK: STATE_UNKNOWN,
    }
    assert not full_mock.called
    cfg = hass.data[DOMAIN]["entry_configs"][entry.entry_id]
    assert cfg.name == "Grid"


async def test_options_flow_keeps_title_in_sync_with_name(recorder):
    # Creation sets title=name; nothing else did, so editing Name in options
    # used to rename the chart series while leaving the Helpers row's title
    # stale forever.
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
        return_value=0,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Grid",
                CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
                CONF_BLANK: STATE_UNKNOWN,
            },
        )
        await hass.async_block_till_done()

    assert entry.title == "Grid"


async def test_the_blank_setting_is_carried_into_the_entry(recorder):
    """A text sensor whose blank means "no error" needs a name, not a preset."""
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_ID: ENTITY,
            CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
            CONF_BLANK: "ok",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_BLANK] == "ok"


@pytest.mark.parametrize(
    ("value", "error"),
    [("record", "blank_record"), ("", "blank_unusable"), ("!!!", "blank_unusable")],
)
async def test_an_unusable_blank_keeps_the_form_open(recorder, value, error):
    """An error on the field, not an abort: the dialog is still fillable."""
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENTITY_ID: ENTITY, CONF_DEFAULT: DEFAULT_RECORD_KNOWN, CONF_BLANK: value},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BLANK: error}


async def test_changing_blank_recompiles_the_whole_history(recorder):
    """It reattributes every past state, exactly as `default` does."""
    hass = recorder
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: ENTITY},
        options={
            CONF_NAME: "Grid Status",
            CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
            CONF_BLANK: STATE_UNKNOWN,
        },
        unique_id=ENTITY,
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile",
        return_value=0,
    ) as full:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Grid Status",
                CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
                CONF_BLANK: "ok",
            },
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert full.called
    assert entry.options[CONF_BLANK] == "ok"
