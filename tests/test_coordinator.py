"""The refresh behind every period sensor on an entry."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.discrete_statistics import rows as rows_module
from custom_components.discrete_statistics.compiler import compiled_signal
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
ON_TODAY = "on-today"
OFF_COUNT_TODAY = "off-count-today"


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


def sensor(subentry_id, states, metric="duration", period="today", live=True):
    return ConfigSubentryData(
        data={"states": states, "metric": metric, "period": period, "live": live},
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_SENSOR,
        title=subentry_id,
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
    # Every test here drives a coordinator of its own, so the platform's is
    # left out: it answers the same compile signal, and the reads a test
    # counts would then be two coordinators' worth.
    with (
        patch("custom_components.discrete_statistics.PLATFORMS", []),
        patch(
            "custom_components.discrete_statistics.Compiler.async_compile_incremental",
            return_value=0,
        ),
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


async def past_the_cooldown(hass, freezer, when):
    """Let a state change's debounced refresh land, at `when` on the clock.

    The refresh is a cooldown behind the change, so the timer has to be
    fired for it; the freezer stays at `when` so the tail is measured to
    the same instant the change happened.
    """
    freezer.move_to(when)
    async_fire_time_changed(hass, when + timedelta(seconds=REFRESH_COOLDOWN + 1))
    await hass.async_block_till_done(wait_background_tasks=True)


async def compile_by_hand(hass, start):
    cfg = next(iter(hass.data[DOMAIN]["entry_configs"].values()))
    await hass.data[DOMAIN]["compiler"].async_compile(cfg, start.timestamp())
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()


async def test_readings_per_subentry(recorder, freezer):
    hass = recorder
    await play(
        hass,
        freezer,
        [
            (T0 - timedelta(hours=1), "off"),
            (T0 + timedelta(hours=1), "on"),
            (T0 + timedelta(hours=1, minutes=30), "off"),
            (T0 + timedelta(hours=2), "on"),
        ],
    )
    freezer.move_to(T0 + timedelta(hours=3))
    entry = await setup_entry(
        hass, [sensor(ON_TODAY, ["on"]), sensor(OFF_COUNT_TODAY, ["off"], "count")]
    )
    await compile_by_hand(hass, T0 - timedelta(hours=1))
    coordinator = PeriodCoordinator(hass, entry, hass.data[DOMAIN]["compiler"])
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data[ON_TODAY].value == 1.5
    assert coordinator.data[OFF_COUNT_TODAY].value == 1
    assert coordinator.compiled_until == (T0 + timedelta(hours=3)).timestamp()


async def test_a_state_change_refreshes_the_live_tail(recorder, freezer):
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=1), "off"), (T0, "on")])
    freezer.move_to(T0 + timedelta(hours=1))
    entry = await setup_entry(hass, [sensor(ON_TODAY, ["on"])])
    await compile_by_hand(hass, T0 - timedelta(hours=1))
    coordinator = PeriodCoordinator(hass, entry, hass.data[DOMAIN]["compiler"])
    await coordinator.async_refresh()
    assert coordinator.data[ON_TODAY].value == 1.0

    freezer.move_to(T0 + timedelta(hours=1, minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await past_the_cooldown(hass, freezer, T0 + timedelta(hours=1, minutes=30))
    assert coordinator.data[ON_TODAY].value == 1.5


async def test_a_compile_refreshes_the_frame(recorder, freezer):
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=1), "off"), (T0, "on")])
    freezer.move_to(T0 + timedelta(hours=1))
    entry = await setup_entry(hass, [sensor(ON_TODAY, ["on"], live=False)])
    coordinator = PeriodCoordinator(hass, entry, hass.data[DOMAIN]["compiler"])
    await coordinator.async_refresh()
    # Nothing compiled: no value, and not a failure either.
    assert coordinator.last_update_success
    assert coordinator.data[ON_TODAY].value is None
    assert coordinator.compiled_until is None

    await compile_by_hand(hass, T0 - timedelta(hours=1))
    await hass.async_block_till_done()
    assert coordinator.data[ON_TODAY].value == 1.0
    assert coordinator.compiled_until == (T0 + timedelta(hours=1)).timestamp()


async def test_sums_are_fetched_once_per_edge_between_compiles(recorder, freezer):
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=1), "off"), (T0, "on")])
    freezer.move_to(T0 + timedelta(hours=1))
    entry = await setup_entry(hass, [sensor(ON_TODAY, ["on"])])
    await compile_by_hand(hass, T0 - timedelta(hours=1))
    coordinator = PeriodCoordinator(hass, entry, hass.data[DOMAIN]["compiler"])
    with patch(
        "custom_components.discrete_statistics.coordinator.rows.sums_at",
        wraps=rows_module.sums_at,
    ) as sums_at:
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        assert sums_at.call_count == 2  # the period start and the watermark end


async def test_an_unloaded_entry_fails_the_refresh(recorder, freezer):
    hass = recorder
    freezer.move_to(T0)
    entry = await setup_entry(hass, [sensor(ON_TODAY, ["on"])])
    coordinator = PeriodCoordinator(hass, entry, hass.data[DOMAIN]["compiler"])
    hass.data[DOMAIN]["entry_configs"].clear()
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    # Not merely "it raised something": without the guard the refresh dies
    # on an AttributeError and fails just the same.
    assert isinstance(coordinator.last_exception, UpdateFailed)


async def test_a_compile_during_a_refresh_does_not_seed_the_new_cache(
    recorder, freezer
):
    """The race a compile mid-refresh opens.

    The sums read here were read before the compile, so putting them in
    the cache that compile just installed would make the refresh it
    scheduled read hits instead of the rewritten hour.
    """
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=1), "off"), (T0, "on")])
    freezer.move_to(T0 + timedelta(hours=1))
    entry = await setup_entry(hass, [sensor(ON_TODAY, ["on"], live=False)])
    await compile_by_hand(hass, T0 - timedelta(hours=1))
    coordinator = PeriodCoordinator(hass, entry, hass.data[DOMAIN]["compiler"])

    fired = []
    real_sums_at = rows_module.sums_at

    def compile_on_the_first_read(*args, **kwargs):
        if not fired:
            fired.append(True)
            # `sums_at` runs in the executor; the signal is the loop's.
            hass.loop.call_soon_threadsafe(
                async_dispatcher_send, hass, compiled_signal(ENTITY)
            )
        return real_sums_at(*args, **kwargs)

    with patch(
        "custom_components.discrete_statistics.coordinator.rows.sums_at",
        side_effect=compile_on_the_first_read,
    ) as sums_at:
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        # Two edges read by each refresh: the one racing the compile, and
        # the one that compile scheduled, which finds an empty cache.
        assert sums_at.call_count == 4
