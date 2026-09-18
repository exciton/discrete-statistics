"""Following the entity registry: renames, fills and missing entities."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.const import (
    CONF_ENTITY_ID,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntity,
    MockEntityPlatform,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.discrete_statistics import compiler as compiler_module
from custom_components.discrete_statistics.config import CONF_DEFAULT, CONF_FILLED_UNTIL
from custom_components.discrete_statistics.const import (
    DEFAULT_RECORD_KNOWN,
    DOMAIN,
    METRIC_DURATION,
    SUBENTRY_STATE,
)
from custom_components.discrete_statistics.naming import describe
from custom_components.discrete_statistics.payload import metadata_for

from .conftest import ON_TODAY, existing, play, read_sums, sensor

ENTITY = "binary_sensor.grid_status"
NEW = "binary_sensor.grid_status_new"
TEMP = "binary_sensor.grid_status_2"
FILTERED = "sensor.filtered_discrete_binary_sensor_grid_status"
ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"
NEW_ON = "discrete_statistics:binary_sensor_grid_status_new_on_duration"
NEW_OFF = "discrete_statistics:binary_sensor_grid_status_new_off_duration"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Nine hours, comfortably more than TRAILING_HOURS (3), so a compile that
# raced the metadata rename would visibly rebuild a second series rather
# than one indistinguishable trailing window.
HISTORY = [
    (T0, "on"),
    (T0 + timedelta(hours=3), "off"),
    (T0 + timedelta(hours=5), "on"),
    (T0 + timedelta(hours=8), "off"),
]


def notifications(hass):
    return hass.data.get("persistent_notification", {})


async def registered(hass, entity_id, unique_id):
    """A registry entry for `entity_id`, as a device's entity has."""
    domain, object_id = entity_id.split(".")
    entry = er.async_get(hass).async_get_or_create(
        domain, "test", unique_id, suggested_object_id=object_id
    )
    assert entry.entity_id == entity_id
    return entry


async def setup_entry(hass, entity_id=ENTITY, yaml=None, subentries=None):
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, DOMAIN, yaml or {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: entity_id},
        options={CONF_NAME: "Grid Status", CONF_DEFAULT: DEFAULT_RECORD_KNOWN},
        unique_id=entity_id,
        title="Grid Status",
        subentries_data=subentries or [],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    return entry


async def settled(hass):
    """Every listener, background task and recorder write has landed."""
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_renamed_entity_keeps_its_statistics(recorder_utc, freezer):
    """Journey 1: rename an entity we record; one family, nothing split."""
    hass = recorder_utc
    await registered(hass, ENTITY, "grid")
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    entry = await setup_entry(hass)
    before = await read_sums(hass, ON, T0, T0 + timedelta(hours=10))
    # On for T0..T0+3h and again for T0+5h..T0+8h: six hours.
    assert before[-1] == 6.0

    er.async_get(hass).async_update_entity(ENTITY, new_entity_id=NEW)
    await settled(hass)

    assert await existing(hass, ENTITY) == []
    assert await existing(hass, NEW) == sorted(
        [
            NEW_ON,
            NEW_OFF,
            NEW_ON.replace("duration", "count"),
            NEW_OFF.replace("duration", "count"),
        ]
    )
    assert (
        await read_sums(hass, NEW_ON, T0, T0 + timedelta(hours=10), entity_id=NEW)
        == before
    )
    assert entry.data[CONF_ENTITY_ID] == NEW
    assert entry.unique_id == NEW
    assert [c.entity_id for c in hass.data[DOMAIN]["all_configs"]()] == [NEW]
    [note] = [n for n in notifications(hass).values() if "Moved" in n["message"]]
    assert ENTITY in note["message"] and NEW in note["message"]
    assert "4 statistic" in note["message"]

    # The series continues under the new name: another hour, compiled by
    # the ordinary run, lands on the moved sums rather than starting over.
    freezer.move_to(T0 + timedelta(hours=11, minutes=30))
    hass.states.async_set(NEW, "on")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    await hass.data[DOMAIN]["compile_all"]()
    await settled(hass)
    assert (await read_sums(hass, NEW_ON, T0, T0 + timedelta(hours=11), entity_id=NEW))[
        -1
    ] == 6.0
    assert await existing(hass, ENTITY) == []


async def test_a_real_entity_survives_its_remove_and_re_add(recorder_utc, freezer):
    """The genuine rename path: the entity is removed and re-added."""
    hass = recorder_utc

    class Grid(MockEntity):
        _attr_state = "on"

        def flip(self, state):
            self._attr_state = state
            self.async_write_ha_state()

    platform = MockEntityPlatform(hass, domain="binary_sensor")
    grid = Grid(unique_id="grid", name="grid_status")
    freezer.move_to(T0)
    await platform.async_add_entities([grid])
    await hass.async_block_till_done()
    assert grid.entity_id == ENTITY
    for when, state in HISTORY[1:]:
        freezer.move_to(when)
        grid.flip(state)
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    freezer.move_to(T0 + timedelta(hours=10))
    entry = await setup_entry(hass)
    before = await read_sums(hass, ON, T0, T0 + timedelta(hours=10))

    er.async_get(hass).async_update_entity(ENTITY, new_entity_id=NEW)
    await settled(hass)

    assert hass.states.get(ENTITY) is None
    assert hass.states.get(NEW) is not None
    assert await existing(hass, ENTITY) == []
    assert (
        await read_sums(hass, NEW_ON, T0, T0 + timedelta(hours=10), entity_id=NEW)
        == before
    )
    assert entry.data[CONF_ENTITY_ID] == NEW


async def test_a_yaml_entity_is_not_followed(recorder_utc, freezer, caplog):
    """A YAML entity is left alone: the config names it, and we do not edit that."""
    hass = recorder_utc
    await registered(hass, ENTITY, "grid")
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: [{"entity_id": ENTITY}]})
    await hass.data[DOMAIN]["compile_all"]()
    await settled(hass)
    assert await existing(hass, ENTITY) != []

    er.async_get(hass).async_update_entity(ENTITY, new_entity_id=NEW)
    await settled(hass)

    assert await existing(hass, NEW) == []
    assert await existing(hass, ENTITY) != []
    assert "configuration.yaml" in caplog.text
    assert any(
        "configuration.yaml" in n["message"] for n in notifications(hass).values()
    )


async def test_an_unrelated_rename_touches_nothing(recorder_utc, freezer):
    """Another entity's rename reaches no compiler and no entry of ours."""
    hass = recorder_utc
    await registered(hass, ENTITY, "grid")
    await registered(hass, "binary_sensor.other", "other")
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    entry = await setup_entry(hass)
    ids = await existing(hass, ENTITY)

    with patch.object(compiler_module.Compiler, "async_rename") as rename:
        er.async_get(hass).async_update_entity(
            "binary_sensor.other", new_entity_id="binary_sensor.other_2"
        )
        await settled(hass)

    assert not rename.called
    assert await existing(hass, ENTITY) == ids
    assert entry.data[CONF_ENTITY_ID] == ENTITY
    assert notifications(hass) == {} or all(
        "Moved" not in n["message"] for n in notifications(hass).values()
    )


async def test_the_entry_is_updated_only_after_the_rename_committed(
    recorder_utc, freezer, monkeypatch
):
    """The order is load-bearing: the reload's compile reads the moved metadata."""
    hass = recorder_utc
    await registered(hass, ENTITY, "grid")
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    await setup_entry(hass)

    order: list[str] = []
    real_run = compiler_module.RenameTask.run
    real_update = hass.config_entries.async_update_entry

    def run(self, instance):
        real_run(self, instance)
        order.append("committed")

    def update(entry, **kwargs):
        if "unique_id" in kwargs:
            order.append("entry updated")
        return real_update(entry, **kwargs)

    monkeypatch.setattr(compiler_module.RenameTask, "run", run)
    monkeypatch.setattr(hass.config_entries, "async_update_entry", update)

    er.async_get(hass).async_update_entity(ENTITY, new_entity_id=NEW)
    await settled(hass)

    assert order[:2] == ["committed", "entry updated"]


async def test_a_collision_is_reported_and_the_rest_still_move(recorder_utc, freezer):
    """A statistic whose new ID is taken stays put; the notification names it."""
    hass = recorder_utc
    await registered(hass, ENTITY, "grid")
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    entry = await setup_entry(hass)
    async_add_external_statistics(
        hass,
        metadata_for(METRIC_DURATION, NEW_ON, "Elsewhere: On (h)"),
        [{"start": T0, "sum": 9.0}],
    )
    await async_wait_recording_done(hass)

    er.async_get(hass).async_update_entity(ENTITY, new_entity_id=NEW)
    await settled(hass)

    assert await existing(hass, ENTITY) == [ON]
    assert NEW_OFF in await existing(hass, NEW)
    [note] = [n for n in notifications(hass).values() if "Moved" in n["message"]]
    assert "1 statistic(s) already existed" in note["message"]
    assert ON in note["message"]
    assert entry.data[CONF_ENTITY_ID] == NEW


async def test_a_failed_reload_is_reported_with_the_move(recorder_utc, freezer):
    """async_reload returns False rather than raising, so it is read, not caught."""
    hass = recorder_utc
    await registered(hass, ENTITY, "grid")
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    await setup_entry(hass)

    with patch(
        "custom_components.discrete_statistics.async_setup_entry",
        side_effect=ConfigEntryNotReady("no"),
    ):
        er.async_get(hass).async_update_entity(ENTITY, new_entity_id=NEW)
        await settled(hass)

    [note] = [n for n in notifications(hass).values() if "Moved" in n["message"]]
    assert "did not reload" in note["message"]
    assert NEW in note["message"]
    assert await existing(hass, ENTITY) == []


def issue(hass, entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"missing_entity_{entry.entry_id}"
    )


async def test_a_removed_entity_raises_a_repair_issue(recorder_utc, freezer):
    """Journey 3: the entity goes; an unavailable one does not count."""
    hass = recorder_utc
    await registered(hass, ENTITY, "grid")
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    entry = await setup_entry(hass)
    assert issue(hass, entry) is None

    hass.states.async_set(ENTITY, "unavailable")
    await settled(hass)
    assert issue(hass, entry) is None

    # A device being removed: its entity leaves the state machine and
    # the registry, in whichever order.
    hass.states.async_remove(ENTITY)
    await settled(hass)
    assert issue(hass, entry) is None  # still registered
    er.async_get(hass).async_remove(ENTITY)
    await settled(hass)

    found = issue(hass, entry)
    assert found is not None
    assert found.is_fixable is False
    assert found.severity == ir.IssueSeverity.WARNING
    assert found.translation_placeholders == {"entity": f"Grid Status ({ENTITY})"}

    # Re-registering the ID clears it.
    await registered(hass, ENTITY, "grid-2")
    await settled(hass)
    assert issue(hass, entry) is None


async def test_a_replacement_renamed_onto_our_entity_is_filled_in(
    recorder_utc, freezer
):
    """Journey 2: the device swap."""
    hass = recorder_utc
    await registered(hass, ENTITY, "grid")
    await play(hass, freezer, HISTORY)  # last transition: off at T0+8h
    freezer.move_to(T0 + timedelta(hours=10))
    entry = await setup_entry(hass)

    # The old device dies at T0+10h30.
    freezer.move_to(T0 + timedelta(hours=10, minutes=30))
    hass.states.async_remove(ENTITY)
    er.async_get(hass).async_remove(ENTITY)
    await settled(hass)
    assert issue(hass, entry) is not None
    # The hourly runs carry on meanwhile.
    freezer.move_to(T0 + timedelta(hours=13, minutes=3))
    await hass.data[DOMAIN]["compile_all"]()
    await settled(hass)

    # The replacement, set up under a temporary ID: on at T0+12h, off at T0+14h.
    await registered(hass, TEMP, "grid-2")
    await play(
        hass,
        freezer,
        [(T0 + timedelta(hours=12), "on"), (T0 + timedelta(hours=14), "off")],
        entity_id=TEMP,
    )
    # The scheduled run must land before the fill, not behind it.
    freezer.move_to(T0 + timedelta(hours=15, minutes=20))
    await settled(hass)

    er.async_get(hass).async_update_entity(TEMP, new_entity_id=ENTITY)
    await settled(hass)

    # The replacement's hours are in our series, under our IDs only.
    assert await existing(hass, TEMP) == []
    on = await read_sums(hass, ON, T0, T0 + timedelta(hours=15))
    off = await read_sums(hass, OFF, T0, T0 + timedelta(hours=15))
    assert on == sorted(on) and off == sorted(off)  # monotonic across the seam
    # hours 0-14: on, on, on, off, off, on, on, on, off, off, off, off (dead,
    # carried from T0+8h), then the replacement's own on, on, off from the fill.
    assert on == [
        1.0,
        2.0,
        3.0,
        3.0,
        3.0,
        4.0,
        5.0,
        6.0,
        6.0,
        6.0,
        6.0,
        6.0,
        7.0,
        8.0,
        8.0,
    ]
    assert off == [
        0.0,
        0.0,
        0.0,
        1.0,
        2.0,
        2.0,
        2.0,
        2.0,
        3.0,
        4.0,
        5.0,
        6.0,
        6.0,
        6.0,
        7.0,
    ]
    # T0+12h and T0+13h are `on`: the sum grows by one an hour there.
    assert on[13] - on[12] == 1.0 and on[14] - on[13] == 0.0
    assert off[14] - off[13] == 1.0
    assert issue(hass, entry) is None
    assert entry.data[CONF_ENTITY_ID] == ENTITY
    [note] = [n for n in notifications(hass).values() if "Filled" in n["message"]]
    assert TEMP in note["message"]
    assert "3 hour(s)" in note["message"]

    # The stale state the registry-only rename leaves under TEMP never
    # opened the fill's window: it is the replacement's own history that
    # did. (No `Entity` removed it; in production it is gone.)
    assert hass.states.get(TEMP) is not None

    assert entry.data[CONF_FILLED_UNTIL] == (T0 + timedelta(hours=15)).timestamp()

    # What a real entity does at the rename: report its own state under our
    # ID. Off carries from the fill's last hour, so hour 15 is 20 minutes
    # off then 40 minutes on.
    hass.states.async_set(ENTITY, "on")
    freezer.move_to(T0 + timedelta(hours=16, minutes=3))
    await hass.data[DOMAIN]["compile_all"]()
    await settled(hass)

    on = await read_sums(hass, ON, T0, T0 + timedelta(hours=16))
    off = await read_sums(hass, OFF, T0, T0 + timedelta(hours=16))
    # Hours 12-14 unchanged from the fill: the floor kept the trailing
    # window from reaching back over them and flattening them to `off`.
    assert on[12:15] == [7.0, 8.0, 8.0]
    assert off[12:15] == [6.0, 6.0, 7.0]
    assert on[15] == pytest.approx(8.0 + 2 / 3)
    assert off[15] == pytest.approx(7.0 + 1 / 3)


async def test_a_fill_that_read_our_own_history_records_no_floor(recorder_utc, freezer):
    """The recorder moved the history onto our ID, so those hours are rebuildable."""
    hass = recorder_utc
    await registered(hass, TEMP, "grid-2")
    # The replacement's history, already under our ID: the recorder's own
    # rename listener met nothing of ours in the states table.
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    entry = await setup_entry(hass)
    # The old device is gone: only its recorded history is left.
    hass.states.async_remove(ENTITY)
    await settled(hass)

    er.async_get(hass).async_update_entity(TEMP, new_entity_id=ENTITY)
    await settled(hass)

    [note] = [n for n in notifications(hass).values() if "Filled" in n["message"]]
    assert TEMP in note["message"]
    assert CONF_FILLED_UNTIL not in entry.data


async def test_a_failed_fill_is_reported_and_records_no_floor(recorder_utc, freezer):
    """Nothing was written, so nothing may be fenced off from a later compile."""
    hass = recorder_utc
    await registered(hass, TEMP, "grid-2")
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    entry = await setup_entry(hass)
    hass.states.async_remove(ENTITY)
    await settled(hass)

    with patch.object(
        compiler_module.Compiler, "async_fill", side_effect=RuntimeError("boom")
    ):
        er.async_get(hass).async_update_entity(TEMP, new_entity_id=ENTITY)
        await settled(hass)

    [note] = [
        n for n in notifications(hass).values() if "Could not fill" in n["message"]
    ]
    assert "boom" in note["message"]
    assert CONF_FILLED_UNTIL not in entry.data


async def test_a_rename_onto_a_yaml_entity_is_logged(recorder_utc, freezer, caplog):
    """A YAML entity has no entry to record the fill floor in, so it is not filled."""
    hass = recorder_utc
    await registered(hass, TEMP, "grid-2")
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: [{"entity_id": ENTITY}]})
    await hass.data[DOMAIN]["compile_all"]()
    hass.states.async_remove(ENTITY)
    await settled(hass)

    with patch.object(compiler_module.Compiler, "async_fill") as fill:
        er.async_get(hass).async_update_entity(TEMP, new_entity_id=ENTITY)
        await settled(hass)

    assert not fill.called
    assert "configuration.yaml" in caplog.text
    assert TEMP in caplog.text


async def test_a_missing_entity_is_reviewed_when_its_entry_is_set_up(
    recorder_utc, freezer
):
    """An entry created after startup names an entity that is already gone."""
    hass = recorder_utc
    freezer.move_to(T0)

    entry = await setup_entry(hass, entity_id="binary_sensor.gone")

    assert issue(hass, entry) is not None


async def test_the_issue_waits_for_home_assistant_to_start(recorder_utc, freezer):
    hass = recorder_utc
    freezer.move_to(T0)
    hass.set_state(CoreState.starting)
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: ENTITY},
        options={CONF_NAME: "Grid Status", CONF_DEFAULT: DEFAULT_RECORD_KNOWN},
        unique_id=ENTITY,
        title="Grid Status",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settled(hass)
    # Nothing has loaded the entity yet: no issue while starting.
    assert issue(hass, entry) is None

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await settled(hass)
    assert issue(hass, entry) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await settled(hass)
    assert issue(hass, entry) is None


def a_device(hass, name):
    """A device of another integration's, as that integration builds one."""
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("test", name)},
        name=name,
    )


async def on_a_device(hass, name, entity_id=ENTITY):
    """A device with the source entity on it."""
    device = a_device(hass, name)
    domain, object_id = entity_id.split(".")
    er.async_get(hass).async_get_or_create(
        domain,
        "test",
        entity_id,
        suggested_object_id=object_id,
        device_id=device.id,
    )
    return device


async def with_a_sensor(hass, freezer, title="Grid Status Today"):
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    return await setup_entry(hass, subentries=[sensor(title, ["on"])])


def sensor_entry(hass):
    return er.async_get(hass).async_get(ON_TODAY)


async def test_a_source_moved_to_another_device_moves_the_sensors(
    recorder_utc, freezer
):
    hass = recorder_utc
    await on_a_device(hass, "Grid")
    other = a_device(hass, "Meter")
    await with_a_sensor(hass, freezer)
    assert sensor_entry(hass).original_name == "Status Today"

    er.async_get(hass).async_update_entity(ENTITY, device_id=other.id)
    await settled(hass)

    after = sensor_entry(hass)
    assert after.device_id == other.id
    assert after.has_entity_name is False
    assert after.original_name == "Grid Status Today"


async def test_a_source_that_gains_a_device_takes_the_sensors_onto_it(
    recorder_utc, freezer
):
    """The device arrives after the sensor exists: only a reload writes it."""
    hass = recorder_utc
    await registered(hass, ENTITY, "grid")
    await with_a_sensor(hass, freezer)
    assert sensor_entry(hass).device_id is None

    device = a_device(hass, "Grid")
    er.async_get(hass).async_update_entity(ENTITY, device_id=device.id)
    await settled(hass)

    after = sensor_entry(hass)
    assert after.device_id == device.id
    assert after.has_entity_name is True
    assert after.original_name == "Status Today"


async def test_a_source_taken_off_its_device_gives_the_sensors_their_name_back(
    recorder_utc, freezer
):
    hass = recorder_utc
    device = await on_a_device(hass, "Grid")
    await with_a_sensor(hass, freezer)
    assert sensor_entry(hass).device_id == device.id

    er.async_get(hass).async_update_entity(ENTITY, device_id=None)
    await settled(hass)

    after = sensor_entry(hass)
    assert after.device_id is None
    assert after.has_entity_name is False
    assert after.original_name == "Grid Status Today"


async def test_a_registry_update_we_do_not_care_about_reloads_nothing(
    recorder_utc, freezer
):
    """The branch must not fire on every write the registry makes."""
    hass = recorder_utc
    await on_a_device(hass, "Grid")
    entry = await with_a_sensor(hass, freezer)

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        er.async_get(hass).async_update_entity(ENTITY, icon="mdi:flash")
        await settled(hass)

    assert not reload.called
    assert entry.state is ConfigEntryState.LOADED


async def test_a_renamed_source_carries_its_sensors_onto_its_new_device(
    recorder_utc, freezer
):
    """The rename path already reloads, so the device link follows from it.

    A rename and a move in one registry update is one event carrying both
    changes, so it takes the rename branch - and only the reload inside
    `async_follow` can land the sensor on the new device.
    """
    hass = recorder_utc
    await on_a_device(hass, "Grid")
    other = a_device(hass, "Grid Status")
    await with_a_sensor(hass, freezer)
    assert sensor_entry(hass).original_name == "Status Today"

    er.async_get(hass).async_update_entity(
        ENTITY, new_entity_id=NEW, device_id=other.id
    )
    await settled(hass)

    after = sensor_entry(hass)
    assert after.device_id == other.id
    assert after.has_entity_name is True
    assert after.original_name == "Today"


def a_state_subentry(title, name=None):
    return ConfigSubentryData(
        data={CONF_NAME: name},
        subentry_id=title,
        subentry_type=SUBENTRY_STATE,
        title=title,
        unique_id=None,
    )


async def a_named_source(hass, name="Grid", entity_id=ENTITY):
    """A registry entry whose own name is what the titles are composed from."""
    domain, object_id = entity_id.split(".")
    return er.async_get(hass).async_get_or_create(
        domain,
        "test",
        entity_id,
        suggested_object_id=object_id,
        original_name=name,
    )


async def unnamed_entry(hass, freezer, subentries):
    """An entry with no typed name, so every title follows the source's."""
    await play(hass, freezer, HISTORY)
    freezer.move_to(T0 + timedelta(hours=10))
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: ENTITY},
        options={CONF_DEFAULT: DEFAULT_RECORD_KNOWN},
        unique_id=ENTITY,
        title=describe(hass, ENTITY),
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settled(hass)
    return entry


async def test_a_renamed_source_recomposes_a_period_sensors_title(
    recorder_utc, freezer
):
    """ "Grid on time today" must not still say Grid once Grid is Cooker."""
    hass = recorder_utc
    await a_named_source(hass)
    entry = await unnamed_entry(hass, freezer, [sensor("Grid on time today", ["on"])])
    assert sensor_entry(hass).original_name == "Grid on time today"

    er.async_get(hass).async_update_entity(ENTITY, name="Cooker")
    await settled(hass)

    assert entry.subentries["Grid on time today"].title == "Cooker on time today"
    assert sensor_entry(hass).original_name == "Cooker on time today"


async def test_a_renamed_source_recomposes_the_state_sensors_title(
    recorder_utc, freezer
):
    hass = recorder_utc
    await a_named_source(hass)
    entry = await unnamed_entry(hass, freezer, [a_state_subentry("Grid state")])
    registry = er.async_get(hass)
    assert registry.async_get(FILTERED).original_name == "Grid state"

    registry.async_update_entity(ENTITY, name="Cooker")
    await settled(hass)

    assert entry.subentries["Grid state"].title == "Cooker state"
    assert registry.async_get(FILTERED).original_name == "Cooker state"


async def test_a_source_relabelled_by_its_integration_recomposes_the_titles(
    recorder_utc, freezer
):
    """`original_name` is half of what `display_name` resolves, so it counts.

    A device re-provisioned or relabelled upstream moves the displayed name
    without anyone typing anything, and the titles must follow it.
    """
    hass = recorder_utc
    await a_named_source(hass)
    entry = await unnamed_entry(hass, freezer, [sensor("Grid on time today", ["on"])])

    er.async_get(hass).async_update_entity(ENTITY, original_name="Cooker")
    await settled(hass)

    assert entry.title == f"Cooker ({ENTITY})"
    assert entry.subentries["Grid on time today"].title == "Cooker on time today"
    assert sensor_entry(hass).original_name == "Cooker on time today"


async def test_a_typed_subentry_name_survives_a_rename_of_the_source(
    recorder_utc, freezer
):
    """A name someone typed is what they asked for; it is never recomposed."""
    hass = recorder_utc
    await a_named_source(hass)
    entry = await unnamed_entry(
        hass,
        freezer,
        [
            sensor("My meter", ["on"], name="My meter"),
            a_state_subentry("My state", name="My state"),
        ],
    )

    er.async_get(hass).async_update_entity(ENTITY, name="Cooker")
    await settled(hass)

    assert entry.subentries["My meter"].title == "My meter"
    assert entry.subentries["My state"].title == "My state"


async def test_a_renamed_source_recomposes_the_entry_title(recorder_utc, freezer):
    """The entry row carries the display name, so it follows too."""
    hass = recorder_utc
    await a_named_source(hass)
    entry = await unnamed_entry(hass, freezer, [])
    assert entry.title == f"Grid ({ENTITY})"

    er.async_get(hass).async_update_entity(ENTITY, name="Cooker")
    await settled(hass)

    assert entry.title == f"Cooker ({ENTITY})"


async def test_a_renamed_source_does_not_recompute(recorder_utc, freezer):
    """A title is not attribution.

    `_async_entry_updated` rebuilds the whole history when the EntityConfig
    moves, so a rename reaching that path would have a large installation
    recompiling every recorded entity because somebody edited a name.
    """
    hass = recorder_utc
    await a_named_source(hass)
    entry = await unnamed_entry(hass, freezer, [sensor("Grid on time today", ["on"])])

    with patch("custom_components.discrete_statistics.Compiler.async_compile") as full:
        er.async_get(hass).async_update_entity(ENTITY, name="Cooker")
        await settled(hass)

    assert not full.called
    assert entry.title == f"Cooker ({ENTITY})"
    assert entry.subentries["Grid on time today"].title == "Cooker on time today"


async def test_a_registry_update_that_changes_no_name_writes_nothing(
    recorder_utc, freezer
):
    """A no-op rename must cost nothing: no subentry write, no listener."""
    hass = recorder_utc
    await a_named_source(hass)
    entry = await unnamed_entry(hass, freezer, [sensor("Grid on time today", ["on"])])

    with patch.object(
        hass.config_entries,
        "async_update_subentry",
        wraps=hass.config_entries.async_update_subentry,
    ) as written:
        er.async_get(hass).async_update_entity(ENTITY, name="Grid")
        await settled(hass)
        er.async_get(hass).async_update_entity(ENTITY, original_name="Grid Status")
        await settled(hass)

    assert not written.called
    assert entry.subentries["Grid on time today"].title == "Grid on time today"


async def a_named_source_on_a_device(hass, device_name, entity_name):
    """A source on a device, named so a title can compose from both."""
    device = a_device(hass, device_name)
    domain, object_id = ENTITY.split(".")
    er.async_get(hass).async_get_or_create(
        domain,
        "test",
        ENTITY,
        suggested_object_id=object_id,
        device_id=device.id,
        original_name=entity_name,
    )
    return device


async def test_a_rename_across_the_prefix_boundary_updates_the_registry_flag(
    recorder_utc, freezer
):
    """The stripping decision lives in the registry, and it has to follow.

    Core writes `has_entity_name` only at platform registration
    (entity_platform.py:1025) - the state write syncs `original_name` and
    not this - so a rename that stops the device's name stripping leaves
    the registry claiming a device-relative name the entity no longer has.
    The device rename in the frontend reads the flag to decide which
    entities to rewrite by hand, so a stale one types a name onto a sensor
    that should have gone on following its source.
    """
    hass = recorder_utc
    await a_named_source_on_a_device(hass, "Grid", "Grid Status")
    await unnamed_entry(hass, freezer, [sensor("Grid Status on time today", ["on"])])
    assert sensor_entry(hass).has_entity_name is True
    assert hass.states.get(ON_TODAY).name == "Grid Status on time today"

    er.async_get(hass).async_update_entity(ENTITY, original_name="Backup Status")
    await settled(hass)

    after = sensor_entry(hass)
    assert after.has_entity_name is False
    assert after.original_name == "Backup Status on time today"
    # What a person sees is unchanged by the flag: core prefixes the device
    # name for any device entity nobody has named, and strips the prefix
    # itself when the flag is False. Pinned so the strip in `entity_naming`
    # is known to still agree with it.
    assert hass.states.get(ON_TODAY).name == "Grid Backup Status on time today"


async def test_a_rename_onto_the_prefix_updates_the_registry_flag_back(
    recorder_utc, freezer
):
    """The other direction: the flag has to go back to True as well."""
    hass = recorder_utc
    await a_named_source_on_a_device(hass, "Grid", "Backup Status")
    await unnamed_entry(hass, freezer, [sensor("Backup Status on time today", ["on"])])
    assert sensor_entry(hass).has_entity_name is False

    er.async_get(hass).async_update_entity(ENTITY, original_name="Grid Status")
    await settled(hass)

    after = sensor_entry(hass)
    assert after.has_entity_name is True
    assert after.original_name == "Status on time today"
    assert hass.states.get(ON_TODAY).name == "Grid Status on time today"


async def test_the_filtered_state_sensor_follows_the_prefix_boundary_too(
    recorder_utc, freezer
):
    """Both sensor classes write the flag, and they do it the same way."""
    hass = recorder_utc
    await a_named_source_on_a_device(hass, "Grid", "Grid Status")
    await unnamed_entry(hass, freezer, [a_state_subentry("Grid Status state")])
    registry = er.async_get(hass)
    assert registry.async_get(FILTERED).has_entity_name is True

    registry.async_update_entity(ENTITY, original_name="Backup Status")
    await settled(hass)

    after = registry.async_get(FILTERED)
    assert after.has_entity_name is False
    assert after.original_name == "Backup Status state"
