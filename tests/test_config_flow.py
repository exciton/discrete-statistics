"""Tests for the config flow."""

from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_ENTITY_ID,
    CONF_NAME,
)
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


NUMERIC = "sensor.living_room_temperature"
ENUM = "sensor.washing_machine_status"


async def _submit(hass, entity_id):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_ID: entity_id,
            CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
            CONF_BLANK: STATE_UNKNOWN,
        },
    )


async def test_a_measuring_entity_is_refused(recorder, entity_registry):
    """Each distinct reading would become its own pair of statistics.

    It no longer fails loudly - a numeric state builds a perfectly valid ID -
    so nothing else would stop it: hundreds of statistics, written densely,
    forever, with 21.5 and 2.15 sharing one because the token keeps only
    digits.
    """
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    entity_registry.async_get_or_create(
        "sensor", "demo", "temp-1",
        suggested_object_id="living_room_temperature",
        capabilities={"state_class": "measurement"},
        unit_of_measurement="°C",
    )

    result = await _submit(hass, NUMERIC)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENTITY_ID: "continuous_state"}


async def test_a_unit_alone_is_enough_to_refuse(recorder, entity_registry):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    entity_registry.async_get_or_create(
        "sensor", "demo", "temp-2",
        suggested_object_id="living_room_temperature",
        unit_of_measurement="°C",
    )

    result = await _submit(hass, NUMERIC)
    assert result["errors"] == {CONF_ENTITY_ID: "continuous_state"}


async def test_an_enum_sensor_is_accepted(recorder, entity_registry):
    """The false-positive guard, and the case a careless filter breaks.

    An enum `sensor.*` has no unit and no state class, and is a primary use
    case - a domain allowlist would have excluded it.
    """
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    entity_registry.async_get_or_create(
        "sensor", "demo", "enum-1",
        suggested_object_id="washing_machine_status",
        original_device_class="enum",
        capabilities={"options": ["idle", "running", "done"]},
    )

    result = await _submit(hass, ENUM)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_unregistered_measuring_entity_is_refused(recorder):
    """A template sensor never registers, so only its live state shows it."""
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    hass.states.async_set(NUMERIC, "21.5", {ATTR_UNIT_OF_MEASUREMENT: "°C"})
    await hass.async_block_till_done()

    result = await _submit(hass, NUMERIC)
    assert result["errors"] == {CONF_ENTITY_ID: "continuous_state"}


async def test_a_measuring_entity_is_refused_while_unavailable(
    recorder, entity_registry
):
    """Attributes are stripped then, so only the registry still shows it."""
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    entity_registry.async_get_or_create(
        "sensor", "demo", "temp-3",
        suggested_object_id="living_room_temperature",
        capabilities={"state_class": "measurement"},
        unit_of_measurement="°C",
    )
    hass.states.async_set(NUMERIC, "unavailable")
    await hass.async_block_till_done()

    result = await _submit(hass, NUMERIC)
    assert result["errors"] == {CONF_ENTITY_ID: "continuous_state"}


async def test_a_binary_sensor_is_still_accepted(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    result = await _submit(hass, ENTITY)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_helper_is_titled_with_the_entitys_name(recorder, entity_registry):
    """The Helpers list shows this, and an entity ID is not what people read."""
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    entity_registry.async_get_or_create(
        "binary_sensor", "demo", "title-1",
        suggested_object_id="grid_status",
        original_name="Mains Power",
    )

    result = await _submit(hass, ENTITY)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Mains Power"


async def test_a_typed_name_still_titles_the_helper(recorder, entity_registry):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    entity_registry.async_get_or_create(
        "binary_sensor", "demo", "title-2",
        suggested_object_id="grid_status",
        original_name="Mains Power",
    )

    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {
            CONF_ENTITY_ID: ENTITY,
            CONF_NAME: "Grid",
            CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
            CONF_BLANK: STATE_UNKNOWN,
        },
    )

    assert result["title"] == "Grid"


async def test_the_entity_id_titles_it_only_as_a_last_resort(recorder):
    """No registry entry and no friendly name."""
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, {})
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    result = await _submit(hass, ENTITY)
    assert result["title"] == ENTITY


async def test_the_options_dialog_says_what_it_is_editing(recorder, entity_registry):
    """Otherwise the form is four fields with nothing naming the subject."""
    hass = recorder
    entity_registry.async_get_or_create(
        "binary_sensor", "demo", "opt-1",
        suggested_object_id="grid_status",
        original_name="Mains Power",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: ENTITY},
        options={
            CONF_NAME: None,
            CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
            CONF_BLANK: STATE_UNKNOWN,
        },
        unique_id=ENTITY,
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["description_placeholders"] == {
        "entity": f"[Mains Power ({ENTITY})](/history?entity_id={ENTITY})",
        "default_name": "Mains Power",
    }


async def test_the_name_box_is_not_prefilled_with_the_default(recorder, entity_registry):
    """A suggested value comes back on submit and would freeze the name.

    The default belongs in the field's description, not in the field: the
    whole point of leaving it blank is that the label follows the entity.
    """
    hass = recorder
    entity_registry.async_get_or_create(
        "binary_sensor", "demo", "opt-2",
        suggested_object_id="grid_status",
        original_name="Mains Power",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: ENTITY},
        options={
            CONF_NAME: None,
            CONF_DEFAULT: DEFAULT_RECORD_KNOWN,
            CONF_BLANK: STATE_UNKNOWN,
        },
        unique_id=ENTITY,
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    suggested = {
        key.schema: key.description.get("suggested_value")
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested.get(CONF_NAME) in (None, "")
