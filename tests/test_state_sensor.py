"""The filtered-state sensor end to end: a `state` subentry in, a state out.

No recorder reads here - the sensor follows the state machine - but the
entry is still set up behind `recorder_mock`, as every entry is.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.discrete_statistics.config import (
    CONF_BLANK,
    CONF_DEFAULT,
    CONF_MIN_DURATION,
    CONF_STATES,
)
from custom_components.discrete_statistics.const import DOMAIN, SUBENTRY_STATE
from tests.conftest import ENTITY, T0, sensor

FILTERED = "sensor.filtered_discrete_binary_sensor_grid_status"


def state_subentry(title="Grid Status state", name=None):
    return ConfigSubentryData(
        data={CONF_NAME: name},
        subentry_id=title,
        subentry_type=SUBENTRY_STATE,
        title=title,
        unique_id=None,
    )


async def setup(hass, options=None, subentries=None, entity_id=ENTITY):
    """An entry with a state subentry and no compiling."""
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: entity_id},
        options={
            CONF_NAME: "Grid Status",
            CONF_DEFAULT: "record_known",
            **(options or {}),
        },
        unique_id=entity_id,
        title="Grid Status",
        subentries_data=subentries if subentries is not None else [state_subentry()],
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def set_state(hass, freezer, when, state, entity_id=ENTITY):
    freezer.move_to(when)
    hass.states.async_set(entity_id, state)
    await hass.async_block_till_done()


async def test_the_sensor_reports_the_recorded_state(recorder_utc, freezer):
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "on")
    await setup(hass)
    assert hass.states.get(FILTERED).state == "on"
    await set_state(hass, freezer, T0 + timedelta(minutes=1), "off")
    assert hass.states.get(FILTERED).state == "off"


async def test_nothing_recordable_yet_reads_unknown(recorder_utc, freezer):
    # Not unavailable: the entity may be perfectly healthy and simply in a
    # state this entry ignores.
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "unavailable")
    await setup(hass)
    assert hass.states.get(FILTERED).state == STATE_UNKNOWN


@pytest.mark.parametrize("ignored", ["unavailable", "unknown"])
async def test_an_ignored_state_never_reaches_the_sensor(
    recorder_utc, freezer, ignored
):
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "on")
    await setup(hass)
    await set_state(hass, freezer, T0 + timedelta(minutes=1), ignored)
    assert hass.states.get(FILTERED).state == "on"


async def test_an_ignored_state_writes_nothing_at_all(recorder_utc, freezer):
    # Not merely the same value: the same State object, so no recorder row
    # and no `last_updated` of its own. The entity being filtered is the
    # chatty one; this must not become chatty with it.
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "on")
    await setup(hass)
    before = hass.states.get(FILTERED)
    for offset, raw in ((1, "unavailable"), (2, "unknown"), (3, "unavailable")):
        await set_state(hass, freezer, T0 + timedelta(minutes=offset), raw)
    after = hass.states.get(FILTERED)
    assert after.state == "on"
    assert after.last_updated == before.last_updated


async def test_the_entity_leaving_the_state_machine_is_carried_across(
    recorder_utc, freezer
):
    # Reloading the entity's own YAML from developer tools removes it and
    # adds it back. The listener sees `new_state=None`, which is the blank
    # state, which `blank:` and `record_known` ignore.
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "on")
    await setup(hass)
    before = hass.states.get(FILTERED)

    freezer.move_to(T0 + timedelta(minutes=1))
    hass.states.async_remove(ENTITY)
    await hass.async_block_till_done()
    assert hass.states.get(FILTERED).state == "on"

    # And back, unavailable first as an entity usually is.
    await set_state(hass, freezer, T0 + timedelta(minutes=2), "unavailable")
    await set_state(hass, freezer, T0 + timedelta(minutes=3), "on")
    after = hass.states.get(FILTERED)
    assert after.state == "on"
    assert after.last_updated == before.last_updated


async def test_a_removal_is_recorded_when_blank_names_a_state(recorder_utc, freezer):
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "on")
    await setup(
        hass,
        options={CONF_BLANK: "gone", CONF_STATES: {"gone": "record"}},
    )
    assert hass.states.get(FILTERED).state == "on"
    freezer.move_to(T0 + timedelta(minutes=1))
    hass.states.async_remove(ENTITY)
    await hass.async_block_till_done()
    assert hass.states.get(FILTERED).state == "gone"


async def test_states_are_mapped(recorder_utc, freezer):
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "cool")
    await setup(hass, options={CONF_STATES: {"cool": "cooling", "heat": "cooling"}})
    assert hass.states.get(FILTERED).state == "cooling"
    await set_state(hass, freezer, T0 + timedelta(minutes=1), "heat")
    assert hass.states.get(FILTERED).state == "cooling"


async def test_a_short_spell_is_held_back_and_then_reported(recorder_utc, freezer):
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "on")
    # Past the threshold before the sensor is built, so the spell it opens
    # on is already long enough to record.
    freezer.move_to(T0 + timedelta(minutes=5))
    await setup(
        hass,
        options={CONF_DEFAULT: "ignore_short", CONF_MIN_DURATION: 60.0},
    )
    assert hass.states.get(FILTERED).state == "on"

    await set_state(hass, freezer, T0 + timedelta(minutes=6), "off")
    # Two seconds into the spell it is still `on`: the same provisional
    # verdict the compile of this hour would reach.
    assert hass.states.get(FILTERED).state == "on"

    # Nothing else arrives - the timer is what reports it.
    freezer.move_to(T0 + timedelta(minutes=7, seconds=1))
    async_fire_time_changed(hass, T0 + timedelta(minutes=7, seconds=1))
    await hass.async_block_till_done()
    assert hass.states.get(FILTERED).state == "off"


async def test_a_bounce_inside_the_threshold_never_reaches_the_sensor(
    recorder_utc, freezer
):
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "off")
    freezer.move_to(T0 + timedelta(minutes=5))
    await setup(
        hass,
        options={CONF_DEFAULT: "ignore_short", CONF_MIN_DURATION: 60.0},
    )
    before = hass.states.get(FILTERED)
    await set_state(hass, freezer, T0 + timedelta(minutes=6), "on")
    await set_state(hass, freezer, T0 + timedelta(minutes=6, seconds=2), "off")
    freezer.move_to(T0 + timedelta(minutes=10))
    async_fire_time_changed(hass, T0 + timedelta(minutes=10))
    await hass.async_block_till_done()
    after = hass.states.get(FILTERED)
    assert after.state == "off"
    assert after.last_updated == before.last_updated


async def test_an_ignored_state_does_not_end_the_spell_in_progress(
    recorder_utc, freezer
):
    # The device flickers offline while the contact is bouncing. The
    # `unavailable` is ignored, so it does not cut the new spell short:
    # the spell runs through it and is recorded once it is long enough.
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "off")
    freezer.move_to(T0 + timedelta(minutes=5))
    await setup(
        hass,
        options={
            CONF_DEFAULT: "ignore_short",
            CONF_STATES: {"unavailable": "ignore"},
            CONF_MIN_DURATION: 60.0,
        },
    )
    await set_state(hass, freezer, T0 + timedelta(minutes=6), "on")
    await set_state(hass, freezer, T0 + timedelta(minutes=6, seconds=1), "unavailable")
    assert hass.states.get(FILTERED).state == "off"

    freezer.move_to(T0 + timedelta(minutes=7, seconds=1))
    async_fire_time_changed(hass, T0 + timedelta(minutes=7, seconds=1))
    await hass.async_block_till_done()
    assert hass.states.get(FILTERED).state == "on"


async def test_the_state_is_restored_over_an_unavailable_entity(recorder_utc, freezer):
    # A restart while the entity is still coming up: the restored state
    # stands, rather than the `unavailable` the entity is reporting.
    hass = recorder_utc
    mock_restore_cache(hass, (State(FILTERED, "on"),))
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "unavailable")
    await setup(hass)
    assert hass.states.get(FILTERED).state == "on"


@pytest.mark.parametrize("restored", ["unavailable", "unknown"])
async def test_an_unrecordable_restored_state_is_not_taken_back(
    recorder_utc, freezer, restored
):
    # A shutdown can leave either behind. Taking one back would report the
    # very state the entry's settings say is not recorded.
    hass = recorder_utc
    mock_restore_cache(hass, (State(FILTERED, restored),))
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "unavailable")
    await setup(hass)
    assert hass.states.get(FILTERED).state == STATE_UNKNOWN


async def test_a_restored_state_the_settings_do_record_is_kept(recorder_utc, freezer):
    # `unavailable` as a map target is recorded whatever the default says,
    # so here it is a real canonical state and survives the restore.
    hass = recorder_utc
    mock_restore_cache(hass, (State(FILTERED, "unavailable"),))
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "unavailable")
    await setup(hass, options={CONF_STATES: {"offline": "unavailable"}})
    assert hass.states.get(FILTERED).state == "unavailable"


async def test_the_spell_is_measured_from_the_entity_own_last_changed(
    recorder_utc, freezer
):
    # The entity has been `off` for an hour when the sensor is set up, so
    # the spell is long and recordable at once - not a fresh one starting
    # the second the sensor was added.
    hass = recorder_utc
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "off")
    freezer.move_to(T0 + timedelta(hours=1))
    await setup(
        hass,
        options={CONF_DEFAULT: "ignore_short", CONF_MIN_DURATION: 60.0},
    )
    assert hass.states.get(FILTERED).state == "off"


async def test_a_settings_change_rebuilds_without_reloading(recorder_utc, freezer):
    # An options edit is applied in place rather than reloading the entry,
    # so this is the only thing that tells the sensor its dispositions
    # moved.
    hass = recorder_utc
    entry = await setup(hass)
    freezer.move_to(T0)
    hass.states.async_set(ENTITY, "cool")
    await hass.async_block_till_done()
    assert hass.states.get(FILTERED).state == "cool"

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_STATES: {"cool": "cooling"}},
    )
    await hass.async_block_till_done()
    assert hass.states.get(FILTERED).state == "cooling"


async def test_the_entity_belongs_to_its_subentry(recorder_utc, freezer):
    hass = recorder_utc
    entry = await setup(hass)
    registered = er.async_get(hass).async_get(FILTERED)
    assert registered.unique_id == "Grid Status state"
    assert registered.config_subentry_id == "Grid Status state"
    assert registered.config_entry_id == entry.entry_id


async def test_the_id_is_outside_the_period_sensors_exclude(recorder_utc, freezer):
    # The README asks for `sensor.discrete_*` to be kept out of the
    # recorder. This one is worth recording, so it must not be swept up.
    hass = recorder_utc
    await setup(hass)
    assert not FILTERED.startswith("sensor.discrete_")
    assert hass.states.get(FILTERED) is not None


async def test_removing_the_subentry_removes_the_sensor(recorder_utc, freezer):
    hass = recorder_utc
    entry = await setup(hass)
    assert hass.states.get(FILTERED) is not None
    subentry = next(iter(entry.subentries.values()))
    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()
    assert hass.states.get(FILTERED) is None


async def test_a_state_subentry_alone_builds_no_coordinator(recorder_utc, freezer):
    # The coordinator listens, drains the recorder and reads it once a
    # minute. A state sensor needs none of that, and an entry that has
    # only one should not be paying for it.
    hass = recorder_utc
    with patch(
        "custom_components.discrete_statistics.sensor.PeriodCoordinator"
    ) as coordinator:
        await setup(hass)
    assert not coordinator.called
    assert hass.states.get(FILTERED) is not None


async def test_a_state_subentry_added_later_is_picked_up(recorder_utc, freezer):
    hass = recorder_utc
    entry = await setup(hass, subentries=[sensor("on today", ["on"])])
    assert hass.states.get(FILTERED) is None
    hass.config_entries.async_add_subentry(entry, _subentry(entry))
    await hass.async_block_till_done()
    assert hass.states.get(FILTERED) is not None


def _subentry(entry):
    from homeassistant.config_entries import ConfigSubentry

    return ConfigSubentry(
        data={CONF_NAME: None},
        subentry_type=SUBENTRY_STATE,
        title="Grid Status state",
        unique_id=None,
    )


async def on_a_device(hass, device_name="Grid"):
    """Put the source entity on a device, as its own integration would."""
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("test", device_name)},
        name=device_name,
    )
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "test",
        ENTITY,
        suggested_object_id="grid_status",
        device_id=device.id,
    )
    return device


async def test_a_filtered_sensor_joins_its_source_entitys_device(recorder_utc, freezer):
    hass = recorder_utc
    device = await on_a_device(hass)
    await setup(hass)
    registered = er.async_get(hass).async_get(FILTERED)
    assert registered.device_id == device.id
    assert registered.has_entity_name is True
    assert registered.original_name == "Status state"
    # Home Assistant prefixes the device once.
    assert hass.states.get(FILTERED).name == "Grid Status state"


async def test_a_filtered_sensor_without_a_source_device_keeps_the_whole_name(
    recorder_utc, freezer
):
    hass = recorder_utc
    await setup(hass)
    registered = er.async_get(hass).async_get(FILTERED)
    assert registered.device_id is None
    assert registered.has_entity_name is False
    assert registered.original_name == "Grid Status state"
    assert hass.states.get(FILTERED).name == "Grid Status state"


async def test_a_typed_name_stands_on_a_device_for_the_filtered_sensor(
    recorder_utc, freezer
):
    hass = recorder_utc
    device = await on_a_device(hass)
    await setup(hass, subentries=[state_subentry(name="My meter")])
    registered = er.async_get(hass).async_get(FILTERED)
    assert registered.device_id == device.id
    assert registered.has_entity_name is False
    assert registered.original_name == "My meter"
    # Home Assistant prefixes a device's name onto an entity that does not
    # carry `has_entity_name`, stripping what already matches.
    assert hass.states.get(FILTERED).name == "Grid My meter"


async def test_an_existing_filtered_sensor_gains_the_device_and_the_name(
    recorder_utc, freezer
):
    """The migration, which has no code behind it.

    A sensor registered before the source had a device is attached and
    renamed by the next setup alone: `async_get_or_create` routes an
    entity it already knows through an update with the device, the name
    and the flag the platform passed.
    """
    hass = recorder_utc
    device = await on_a_device(hass)
    registry = er.async_get(hass)
    before = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "Grid Status state",
        suggested_object_id=FILTERED.split(".")[1],
        original_name="Grid Status state",
        has_entity_name=False,
    )
    assert before.entity_id == FILTERED
    assert before.device_id is None

    await setup(hass)

    after = registry.async_get(FILTERED)
    assert after.id == before.id
    assert after.entity_id == before.entity_id
    assert after.device_id == device.id
    assert after.has_entity_name is True
    assert after.original_name == "Status state"
