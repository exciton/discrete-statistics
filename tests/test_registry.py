"""Following the entity registry: renames, fills and missing entities."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
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
from custom_components.discrete_statistics.config import CONF_DEFAULT
from custom_components.discrete_statistics.const import (
    DEFAULT_RECORD_KNOWN,
    DOMAIN,
    METRIC_DURATION,
)
from custom_components.discrete_statistics.payload import metadata_for

from .conftest import existing, play, read_sums

ENTITY = "binary_sensor.grid_status"
NEW = "binary_sensor.grid_status_new"
ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
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


async def setup_entry(hass, entity_id=ENTITY, yaml=None):
    hass.set_state(CoreState.running)
    assert await async_setup_component(hass, DOMAIN, yaml or {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: entity_id},
        options={CONF_NAME: "Grid Status", CONF_DEFAULT: DEFAULT_RECORD_KNOWN},
        unique_id=entity_id,
        title="Grid Status",
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
