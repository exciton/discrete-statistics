"""Period sensors end to end: subentries in, states out."""

from datetime import timedelta
from types import MappingProxyType
from unittest.mock import patch

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.discrete_statistics import rows as rows_module
from custom_components.discrete_statistics.compiler import Compiler, compiled_signal
from custom_components.discrete_statistics.const import DOMAIN, SUBENTRY_SENSOR
from custom_components.discrete_statistics.coordinator import PeriodCoordinator
from tests.conftest import ON_TODAY, T0, changed_state, seeded, sensor

ENTITY = "binary_sensor.grid_status"
OFF_TODAY = "sensor.discrete_binary_sensor_grid_status_off_duration_today"
ON_COUNT = "sensor.discrete_binary_sensor_grid_status_on_count_today"
ON_SHARE = "sensor.discrete_binary_sensor_grid_status_on_share_today"
ON_YESTERDAY = "sensor.discrete_binary_sensor_grid_status_on_duration_yesterday"
ON_COUNT_LAST_HOUR = "sensor.discrete_binary_sensor_grid_status_on_count_last_hour"
ON_LAST_HOUR = "sensor.discrete_binary_sensor_grid_status_on_duration_last_hour"


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


async def test_sensors_read_the_statistics(recorder_utc, freezer):
    hass = recorder_utc
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


async def test_the_entity_belongs_to_its_subentry(recorder_utc, freezer):
    hass = recorder_utc
    entry = await seeded(hass, freezer, [sensor("on today", ["on"])])
    registry = er.async_get(hass)
    registered = registry.async_get(ON_TODAY)
    assert registered.unique_id == "on today"
    assert registered.config_subentry_id == "on today"
    assert registered.config_entry_id == entry.entry_id


async def test_a_live_sensor_follows_the_entity(recorder_utc, freezer):
    hass = recorder_utc
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


async def test_a_tick_where_nothing_changed_writes_nothing(recorder_utc, freezer):
    hass = recorder_utc
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
    recorder_utc, freezer, caplog
):
    hass = recorder_utc
    await seeded(hass, freezer, [sensor("unknown", ["unknown"])])
    entity_id = "sensor.discrete_binary_sensor_grid_status_unknown_duration_today"
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert "not recorded by this entry's settings" in caplog.text


async def test_subentries_come_and_go_with_their_sensors(recorder_utc, freezer):
    hass = recorder_utc
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


async def test_reconfiguring_keeps_the_entity(recorder_utc, freezer):
    hass = recorder_utc
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


async def test_one_update_reaches_every_sensor(recorder_utc, freezer):
    hass = recorder_utc
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


async def test_an_entry_without_sensors_reads_nothing(recorder_utc, freezer):
    hass = recorder_utc
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


async def test_the_last_sensor_leaving_stops_the_reads(recorder_utc, freezer):
    hass = recorder_utc
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
            "custom_components.discrete_statistics.coordinator.rows.sums_at_edges",
            wraps=rows_module.sums_at_edges,
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


async def test_a_reload_releases_the_old_coordinator(recorder_utc, freezer):
    hass = recorder_utc
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


async def test_a_rolling_sensor_slides_with_the_clock(recorder_utc, freezer):
    """The end-to-end form of history_stats' one-hour `duration` measure."""
    hass = recorder_utc
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
    recorder_utc, freezer
):
    hass = recorder_utc
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


async def test_a_part_hour_the_recorder_has_lost_is_estimated(recorder_utc, freezer):
    hass = recorder_utc
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


async def test_a_state_with_no_row_in_a_compiled_period_reads_zero(
    recorder_utc, freezer
):
    hass = recorder_utc
    # The third hour is compiled and wholly "on", so "off" has no row in
    # it: its sum is the one carried from the hour before, not nothing.
    await seeded(
        hass,
        freezer,
        [sensor("off last hour", ["off"], period="last_hour", live=False)],
    )
    state = hass.states.get(
        "sensor.discrete_binary_sensor_grid_status_off_duration_last_hour"
    )
    assert state.state == "0.0"
    assert state.attributes["period_start"] == (T0 + timedelta(hours=2)).isoformat()


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


async def test_a_sensor_joins_its_source_entitys_device(recorder_utc, freezer):
    hass = recorder_utc
    device = await on_a_device(hass)
    await seeded(hass, freezer, [sensor("Grid status today", ["on"])])
    registered = er.async_get(hass).async_get(ON_TODAY)
    assert registered.device_id == device.id
    assert registered.has_entity_name is True
    assert registered.original_name == "Status today"
    # Home Assistant prefixes the device once.
    assert hass.states.get(ON_TODAY).name == "Grid Status today"


async def test_a_sensor_without_a_source_device_keeps_the_whole_name(
    recorder_utc, freezer
):
    hass = recorder_utc
    await seeded(hass, freezer, [sensor("Grid status today", ["on"])])
    registered = er.async_get(hass).async_get(ON_TODAY)
    assert registered.device_id is None
    assert registered.has_entity_name is False
    assert registered.original_name == "Grid status today"
    assert hass.states.get(ON_TODAY).name == "Grid status today"


async def test_a_typed_name_stands_on_a_device(recorder_utc, freezer):
    hass = recorder_utc
    device = await on_a_device(hass)
    await seeded(hass, freezer, [sensor("My meter", ["on"], name="My meter")])
    registered = er.async_get(hass).async_get(ON_TODAY)
    assert registered.device_id == device.id
    assert registered.has_entity_name is False
    assert registered.original_name == "My meter"
    # Home Assistant prefixes a device's name onto an entity that does not
    # carry `has_entity_name`, stripping what already matches.
    assert hass.states.get(ON_TODAY).name == "Grid My meter"


async def test_an_existing_sensor_gains_the_device_and_the_name(recorder_utc, freezer):
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
        "Grid status today",
        suggested_object_id=ON_TODAY.split(".")[1],
        original_name="Grid status today",
        has_entity_name=False,
    )
    assert before.entity_id == ON_TODAY
    assert before.device_id is None

    await seeded(hass, freezer, [sensor("Grid status today", ["on"])])

    after = registry.async_get(ON_TODAY)
    assert after.id == before.id
    assert after.entity_id == before.entity_id
    assert after.device_id == device.id
    assert after.has_entity_name is True
    assert after.original_name == "Status today"


async def test_an_entry_update_that_changes_nothing_costs_no_refresh(
    recorder_utc, freezer
):
    """A device-relative name is not the subentry's title, and apply knows it.

    The naming is what `apply` compares, so an update carrying no change
    to a sensor still reports none - where comparing the title against
    the name would report one on every update.
    """
    hass = recorder_utc
    await on_a_device(hass)
    entry = await seeded(hass, freezer, [sensor("Grid status today", ["on"])])
    refreshes = []
    with counting_refreshes(refreshes), no_hourly_compile():
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "filled_until": 1.0}
        )
        await hass.async_block_till_done()
    assert refreshes == []
