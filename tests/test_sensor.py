"""Period sensors end to end: subentries in, states out."""

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME, STATE_UNAVAILABLE
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.discrete_statistics import rows as rows_module
from custom_components.discrete_statistics.compiler import Compiler, compiled_signal
from custom_components.discrete_statistics.config import CONF_DEFAULT
from custom_components.discrete_statistics.const import (
    DEFAULT_RECORD_KNOWN,
    DOMAIN,
    SUBENTRY_SENSOR,
)
from custom_components.discrete_statistics.coordinator import (
    REFRESH_COOLDOWN,
    PeriodCoordinator,
)

ENTITY = "binary_sensor.grid_status"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ON_TODAY = "sensor.discrete_binary_sensor_grid_status_on_duration_today"
OFF_TODAY = "sensor.discrete_binary_sensor_grid_status_off_duration_today"
ON_COUNT = "sensor.discrete_binary_sensor_grid_status_on_count_today"
ON_SHARE = "sensor.discrete_binary_sensor_grid_status_on_share_today"
ON_YESTERDAY = "sensor.discrete_binary_sensor_grid_status_on_duration_yesterday"
ON_COUNT_LAST_HOUR = "sensor.discrete_binary_sensor_grid_status_on_count_last_hour"
ON_LAST_HOUR = "sensor.discrete_binary_sensor_grid_status_on_duration_last_hour"


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


def sensor(title, states, metric="duration", period="today", live=True, **data):
    return ConfigSubentryData(
        data={
            "states": states,
            "metric": metric,
            "period": period,
            "live": live,
            **data,
        },
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


async def changed_state(hass, freezer, when, state):
    """Set the entity's state at `when` and let the debounced refresh land.

    The refresh a change asks for is a cooldown behind it, so its timer
    has to be fired; the freezer stays at `when` so the tail is measured
    to the instant the change happened.
    """
    freezer.move_to(when)
    hass.states.async_set(ENTITY, state)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, when + timedelta(seconds=REFRESH_COOLDOWN + 1))
    await hass.async_block_till_done(wait_background_tasks=True)


def no_hourly_compile():
    """Hold the hourly compile off while the clock is moved past its turn.

    A compile refreshes the coordinator as well, and the runs below are
    counting the refreshes one state change asks for.
    """
    return patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    )


def counting_refreshes(calls):
    """Patch the refresh so every one of them, on any coordinator, is counted."""
    real = PeriodCoordinator._async_update_data

    async def counted(self):
        calls.append(self)
        return await real(self)

    return patch.object(PeriodCoordinator, "_async_update_data", counted)


def counting_state_changes(calls):
    """Patch the state-change listener so every firing of it is counted.

    A listener is subscribed with the bound method it was built from, so
    this has to be in place before the coordinator is: patching it after
    the fact would leave every existing subscription uncounted, which is
    exactly the thing being counted.
    """
    real = PeriodCoordinator._state_changed

    @callback
    def counted(self, event):
        calls.append(self)
        real(self, event)

    return patch.object(PeriodCoordinator, "_state_changed", counted)


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
    assert on.attributes["estimated"] is False
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
    await changed_state(hass, freezer, T0 + timedelta(hours=3, minutes=20), "off")
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


async def test_the_last_sensor_leaving_stops_the_reads(recorder, freezer):
    hass = recorder
    entry = await seeded(hass, freezer, [sensor("on today", ["on"])])
    hass.config_entries.async_remove_subentry(entry, "on today")
    await hass.async_block_till_done()
    assert hass.states.get(ON_TODAY) is None

    # The coordinator is kept, and so is its listener, so a change still
    # asks it for a refresh; the refresh is what has nothing to do.
    calls = []
    with (
        no_hourly_compile(),
        counting_refreshes(calls),
        patch(
            "custom_components.discrete_statistics.coordinator.get_instance",
            wraps=get_instance,
        ) as recorder_instance,
        patch(
            "custom_components.discrete_statistics.coordinator.rows.sums_at",
            wraps=rows_module.sums_at,
        ) as sums_at,
        patch.object(Compiler, "async_tail") as tail,
    ):
        await changed_state(hass, freezer, T0 + timedelta(hours=3, minutes=20), "off")
    assert len(calls) == 1
    # `get_instance` is the drain and every executor read alike: not one of
    # them reaches the recorder.
    assert recorder_instance.call_count == 0
    assert sums_at.call_count == 0
    assert tail.call_count == 0


async def test_a_reload_releases_the_old_coordinator(recorder, freezer):
    hass = recorder
    changes, refreshes = [], []
    with (
        counting_state_changes(changes),
        counting_refreshes(refreshes),
        no_hourly_compile(),
    ):
        entry = await seeded(hass, freezer, [sensor("on today", ["on"])])
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(ON_TODAY).state == "1.5"

        # From here the reloaded entry answers alone: the subscriptions the
        # unloaded coordinator held were released with it, so the compiler's
        # signal and a change of the entity's state each reach one
        # coordinator and each cost one refresh.
        changes.clear()
        refreshes.clear()
        async_dispatcher_send(
            hass,
            compiled_signal(ENTITY),
            T0.timestamp(),
            (T0 + timedelta(hours=3)).timestamp(),
        )
        await hass.async_block_till_done()
        assert len(refreshes) == 1
        await changed_state(hass, freezer, T0 + timedelta(hours=3, minutes=20), "off")

    assert len(changes) == 1
    assert len(refreshes) == 2
    assert hass.states.get(ON_TODAY).state == "1.83"


async def test_a_rolling_sensor_slides_with_the_clock(recorder, freezer):
    """The end-to-end form of history_stats' one-hour `duration` measure."""
    hass = recorder
    await seeded(
        hass, freezer, [sensor("on count last hour", ["on"], "count", "last_hour")]
    )
    # Three o'clock: the last hour is the third compiled hour, whole, in
    # which the entity turned on once.
    state = hass.states.get(ON_COUNT_LAST_HOUR)
    assert state.state == "1"
    assert state.attributes["period_start"] == (T0 + timedelta(hours=2)).isoformat()
    assert state.attributes["period_end"] == (T0 + timedelta(hours=3)).isoformat()
    assert state.attributes["estimated"] is False

    # Twenty minutes and a half later: the window no longer holds that
    # change. Forty minutes of the third hour are read exactly from the
    # recorder, the rest from the tail; the edges are shown to the minute.
    with no_hourly_compile():
        freezer.move_to(T0 + timedelta(hours=3, minutes=20, seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(ON_COUNT_LAST_HOUR)
    assert state.state == "0"
    assert (
        state.attributes["period_start"]
        == (T0 + timedelta(hours=2, minutes=20)).isoformat()
    )
    assert (
        state.attributes["period_end"]
        == (T0 + timedelta(hours=3, minutes=20)).isoformat()
    )
    assert state.attributes["estimated"] is False


async def test_a_rolling_sensor_that_is_not_live_moves_only_with_a_compile(
    recorder, freezer
):
    hass = recorder
    await seeded(
        hass,
        freezer,
        [sensor("on last hour", ["on"], period="last_hour", live=False)],
    )
    # The last compiled hour, whole: on throughout the third hour.
    state = hass.states.get(ON_LAST_HOUR)
    assert state.state == "1.0"
    assert state.attributes["period_start"] == (T0 + timedelta(hours=2)).isoformat()
    assert state.attributes["period_end"] == (T0 + timedelta(hours=3)).isoformat()

    # Twenty minutes on, with nothing compiled: the window has not moved,
    # so the sensor writes nothing.
    with no_hourly_compile():
        freezer.move_to(T0 + timedelta(hours=3, minutes=20, seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    after = hass.states.get(ON_LAST_HOUR)
    assert after.state == "1.0"
    assert after.attributes["period_start"] == (T0 + timedelta(hours=2)).isoformat()
    assert after.attributes["period_end"] == (T0 + timedelta(hours=3)).isoformat()
    assert after.last_updated == state.last_updated


async def test_a_part_hour_the_recorder_has_lost_is_estimated(recorder, freezer):
    hass = recorder
    await seeded(
        hass, freezer, [sensor("on count last hour", ["on"], "count", "last_hour")]
    )
    with no_hourly_compile():
        freezer.move_to(T0 + timedelta(hours=3, minutes=20))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ON_COUNT_LAST_HOUR).state == "0"

    # Purge has taken everything before three o'clock: the third hour's
    # one change, two thirds of the hour in, is estimated as a whole one.
    with patch.object(
        Compiler,
        "async_earliest_state_ts",
        return_value=(T0 + timedelta(hours=3)).timestamp(),
    ):
        async_dispatcher_send(
            hass,
            compiled_signal(ENTITY),
            (T0 + timedelta(hours=2)).timestamp(),
            (T0 + timedelta(hours=3)).timestamp(),
        )
        await hass.async_block_till_done()
    state = hass.states.get(ON_COUNT_LAST_HOUR)
    assert state.state == "1"
    assert state.attributes["estimated"] is True
