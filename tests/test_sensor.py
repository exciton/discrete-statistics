"""Period sensors end to end: subentries in, states out."""

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME, STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.discrete_statistics.config import CONF_DEFAULT
from custom_components.discrete_statistics.const import (
    DEFAULT_RECORD_KNOWN,
    DOMAIN,
    SUBENTRY_SENSOR,
)
from custom_components.discrete_statistics.coordinator import PeriodCoordinator

ENTITY = "binary_sensor.grid_status"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ON_TODAY = "sensor.discrete_binary_sensor_grid_status_on_duration_today"
OFF_TODAY = "sensor.discrete_binary_sensor_grid_status_off_duration_today"
ON_COUNT = "sensor.discrete_binary_sensor_grid_status_on_count_today"
ON_SHARE = "sensor.discrete_binary_sensor_grid_status_on_share_today"
ON_YESTERDAY = "sensor.discrete_binary_sensor_grid_status_on_duration_yesterday"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override the root conftest fixture; see tests/test_compiler.py."""
    yield


@pytest.fixture
async def recorder(recorder_mock, hass):
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.config.async_set_time_zone("UTC")
    await hass.async_block_till_done()
    return hass


def sensor(title, states, metric="duration", period="today", live=True):
    return ConfigSubentryData(
        data={"states": states, "metric": metric, "period": period, "live": live},
        subentry_id=title,
        subentry_type=SUBENTRY_SENSOR,
        title=title,
        unique_id=None,
    )


async def setup_entry(hass, subentries):
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTITY_ID: ENTITY},
        options={CONF_NAME: "Grid Status", CONF_DEFAULT: DEFAULT_RECORD_KNOWN},
        unique_id=ENTITY,
        title="Grid Status",
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def play(hass, freezer, timeline):
    for when, state in timeline:
        freezer.move_to(when)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    # The recorder's own queue is empty from the moment its thread picks a
    # write up, so wait on a task queued behind it instead.
    await async_wait_recording_done(hass)


async def compile_by_hand(hass, start):
    cfg = next(iter(hass.data[DOMAIN]["entry_configs"].values()))
    await hass.data[DOMAIN]["compiler"].async_compile(cfg, start.timestamp())
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()


TIMELINE = [
    (T0 - timedelta(hours=1), "off"),
    (T0 + timedelta(hours=1), "on"),
    (T0 + timedelta(hours=1, minutes=30), "off"),
    (T0 + timedelta(hours=2), "on"),
]


async def seeded(hass, freezer, subentries):
    """Three compiled hours of today, then the entry with its sensors."""
    await play(hass, freezer, TIMELINE)
    freezer.move_to(T0 + timedelta(hours=3))
    entry = await setup_entry(hass, subentries)
    await compile_by_hand(hass, T0 - timedelta(hours=1))
    return entry


async def test_sensors_read_the_statistics(recorder, freezer):
    hass = recorder
    await seeded(
        hass,
        freezer,
        [
            sensor("on today", ["on"]),
            sensor("on count", ["on"], "count"),
            sensor("on share", ["on"], "share"),
            sensor("on yesterday", ["on"], period="yesterday"),
        ],
    )
    assert hass.states.get(ON_TODAY).state == "1.5"
    assert hass.states.get(ON_COUNT).state == "2"
    assert hass.states.get(ON_SHARE).state == "50.0"
    assert hass.states.get(ON_YESTERDAY).state == "0.0"
    on = hass.states.get(ON_TODAY)
    assert on.attributes["unit_of_measurement"] == "h"
    assert on.attributes["device_class"] == "duration"
    assert on.attributes["period_start"] == T0.isoformat()
    assert on.attributes["period_end"] == (T0 + timedelta(days=1)).isoformat()
    assert on.attributes["compiled_until"] == (T0 + timedelta(hours=3)).isoformat()
    assert on.attributes["live"] is True
    assert on.name == "on today"
    assert hass.states.get(ON_SHARE).attributes["unit_of_measurement"] == "%"
    assert "unit_of_measurement" not in hass.states.get(ON_COUNT).attributes


async def test_the_entity_belongs_to_its_subentry(recorder, freezer):
    hass = recorder
    entry = await seeded(hass, freezer, [sensor("on today", ["on"])])
    registry = er.async_get(hass)
    registered = registry.async_get(ON_TODAY)
    assert registered.unique_id == "on today"
    assert registered.config_subentry_id == "on today"
    assert registered.config_entry_id == entry.entry_id


async def test_a_live_sensor_follows_the_entity(recorder, freezer):
    hass = recorder
    await seeded(
        hass, freezer, [sensor("on today", ["on"]), sensor("off today", ["off"])]
    )
    freezer.move_to(T0 + timedelta(hours=3, minutes=20))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    # Twenty minutes on since the watermark; off only just.
    assert hass.states.get(ON_TODAY).state == "1.83"
    assert hass.states.get(OFF_TODAY).state == "1.5"

    # The minute tick moves the open spell on.
    freezer.move_to(T0 + timedelta(hours=3, minutes=30))
    async_fire_time_changed(hass)
    # The coordinator's interval refresh is a background task, which
    # async_block_till_done leaves alone unless it is asked for it.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ON_TODAY).state == "1.83"
    assert hass.states.get(OFF_TODAY).state == "1.67"


async def test_a_tick_where_nothing_changed_writes_nothing(recorder, freezer):
    hass = recorder
    await seeded(hass, freezer, [sensor("on today", ["on"], live=False)])
    before = hass.states.get(ON_TODAY)
    freezer.move_to(T0 + timedelta(hours=3, minutes=1))
    async_fire_time_changed(hass)
    # The coordinator's interval refresh is a background task, which
    # async_block_till_done leaves alone unless it is asked for it.
    await hass.async_block_till_done(wait_background_tasks=True)
    after = hass.states.get(ON_TODAY)
    assert after.last_updated == before.last_updated


async def test_a_sensor_over_ignored_states_is_unavailable_with_a_warning(
    recorder, freezer, caplog
):
    hass = recorder
    await seeded(hass, freezer, [sensor("unknown", ["unknown"])])
    entity_id = "sensor.discrete_binary_sensor_grid_status_unknown_duration_today"
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert "not recorded by this entry's settings" in caplog.text


async def test_subentries_come_and_go_with_their_sensors(recorder, freezer):
    hass = recorder
    entry = await seeded(hass, freezer, [sensor("on today", ["on"])])
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(
                {
                    "states": ["off"],
                    "metric": "duration",
                    "period": "today",
                    "live": True,
                }
            ),
            subentry_id="off today",
            subentry_type=SUBENTRY_SENSOR,
            title="off today",
            unique_id=None,
        ),
    )
    await hass.async_block_till_done()
    assert hass.states.get(OFF_TODAY).state == "1.5"

    hass.config_entries.async_remove_subentry(entry, "on today")
    await hass.async_block_till_done()
    assert hass.states.get(ON_TODAY) is None
    assert hass.states.get(OFF_TODAY).state == "1.5"


async def test_reconfiguring_keeps_the_entity(recorder, freezer):
    hass = recorder
    entry = await seeded(hass, freezer, [sensor("on today", ["on"])])
    hass.config_entries.async_update_subentry(
        entry,
        entry.subentries["on today"],
        data={"states": ["on"], "metric": "count", "period": "today", "live": True},
        title="on count",
    )
    await hass.async_block_till_done()
    state = hass.states.get(ON_TODAY)
    assert state.state == "2"
    assert state.name == "on count"
    assert "unit_of_measurement" not in state.attributes


async def test_one_update_reaches_every_sensor(recorder, freezer):
    hass = recorder
    entry = await seeded(
        hass, freezer, [sensor("on today", ["on"]), sensor("off today", ["off"])]
    )
    # Home Assistant notifies the listeners once per subentry change, so
    # the event carrying two changed subentries has to be built: the
    # notification is held over both updates, then the listeners are
    # called once, which is what that event is.
    with patch.object(hass.config_entries, "_async_save_and_notify"):
        for subentry_id, state in (("on today", "on"), ("off today", "off")):
            hass.config_entries.async_update_subentry(
                entry,
                entry.subentries[subentry_id],
                data={
                    "states": [state],
                    "metric": "count",
                    "period": "today",
                    "live": True,
                },
                title=f"{state} count",
            )
    for listener in list(entry.update_listeners):
        await listener(hass, entry)
    await hass.async_block_till_done()

    on, off = hass.states.get(ON_TODAY), hass.states.get(OFF_TODAY)
    assert (on.name, on.state) == ("on count", "2")
    assert (off.name, off.state) == ("off count", "1")
    assert "unit_of_measurement" not in off.attributes


async def test_an_entry_without_sensors_reads_nothing(recorder, freezer):
    hass = recorder
    entry = await seeded(hass, freezer, [])
    with patch.object(PeriodCoordinator, "_async_update_data") as refresh:
        hass.states.async_set(ENTITY, "off")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert refresh.call_count == 0

    # The first subentry is what builds the coordinator.
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(
                {
                    "states": ["on"],
                    "metric": "duration",
                    "period": "today",
                    "live": True,
                }
            ),
            subentry_id="on today",
            subentry_type=SUBENTRY_SENSOR,
            title="on today",
            unique_id=None,
        ),
    )
    await hass.async_block_till_done()
    assert hass.states.get(ON_TODAY).state == "1.5"
