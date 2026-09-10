"""The refresh behind every period sensor on an entry."""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import CoreState
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
    render_datetime,
)

ENTITY = "binary_sensor.grid_status"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ON_TODAY = "on-today"
OFF_COUNT_TODAY = "off-count-today"
SINCE_QUARTER_PAST_ONE = "sensor.since_quarter_past_one"
YESTERDAY = "sensor.yesterday"
LAST_HOUR_COUNT = "sensor.last_hour_count"
# Every scenario here compiles the three hours from T0 with this timeline.
TIMELINE = [
    (T0 - timedelta(hours=1), "off"),
    (T0 + timedelta(hours=1), "on"),
    (T0 + timedelta(hours=1, minutes=30), "off"),
    (T0 + timedelta(hours=2), "on"),
]


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


def sensor(subentry_id, states, metric="duration", period="today", live=True, **data):
    return ConfigSubentryData(
        data={
            "states": states,
            "metric": metric,
            "period": period,
            "live": live,
            **data,
        },
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


def custom(subentry_id, start=None, end=None, duration=None, **kwargs):
    return sensor(
        subentry_id,
        ["on"],
        period="custom",
        start=start,
        end=end,
        duration=duration,
        **kwargs,
    )


async def compiled_entry(hass, freezer, subentries):
    """The timeline compiled to three o'clock, then a coordinator over the entry."""
    await play(hass, freezer, TIMELINE)
    freezer.move_to(T0 + timedelta(hours=3))
    entry = await setup_entry(hass, subentries)
    await compile_by_hand(hass, T0 - timedelta(hours=1))
    return PeriodCoordinator(hass, entry, hass.data[DOMAIN]["compiler"])


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
            # `sums_at` runs in the executor and the signal is the loop's,
            # so the read waits for the loop to have delivered it. The
            # race under test is a compile landing after the frame is read
            # and before the sums are written; a signal merely queued from
            # here is delivered whenever the loop next looks, which under
            # load is as easily after this refresh has written both edges
            # - a different race, in which keeping the sum at an edge at
            # or before the compiled range is right and one read is
            # enough.
            delivered = threading.Event()

            def send() -> None:
                async_dispatcher_send(
                    hass,
                    compiled_signal(ENTITY),
                    T0.timestamp(),
                    (T0 + timedelta(hours=3)).timestamp(),
                )
                delivered.set()

            hass.loop.call_soon_threadsafe(send)
            delivered.wait()
        return real_sums_at(*args, **kwargs)

    with patch(
        "custom_components.discrete_statistics.coordinator.rows.sums_at",
        side_effect=compile_on_the_first_read,
    ) as sums_at:
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        # Both edges twice: the refresh racing the compile reads them into
        # the cache it started with, and the refresh that compile
        # scheduled finds the cache that compile installed empty.
        assert sorted(call.args[2] for call in sums_at.call_args_list) == [
            T0.timestamp(),
            T0.timestamp(),
            (T0 + timedelta(hours=1)).timestamp(),
            (T0 + timedelta(hours=1)).timestamp(),
        ]


async def test_a_refresh_before_startup_does_not_wait_for_the_recorder(
    recorder, freezer
):
    """The recorder holds its queue until Home Assistant has started.

    A refresh during setup that waited on it would wait on startup, which
    is itself waiting on the entry - a deadlock the bootstrap breaks only
    by giving up on the entry. Before startup the reads go straight to the
    database; the compile at startup signals a refresh that drains.
    """
    hass = recorder
    freezer.move_to(T0)
    entry = await setup_entry(hass, [sensor(ON_TODAY, ["on"])])
    coordinator = PeriodCoordinator(hass, entry, hass.data[DOMAIN]["compiler"])
    with patch.object(Recorder, "async_block_till_done") as drain:
        hass.set_state(CoreState.starting)
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert drain.call_count == 0
        hass.set_state(CoreState.running)
        await coordinator.async_refresh()
        assert drain.call_count == 1


# --- part hours and custom windows -----------------------------------------


async def test_a_part_hour_is_exact_while_the_recorder_holds_it(recorder, freezer):
    hass = recorder
    coordinator = await compiled_entry(
        hass, freezer, [custom(SINCE_QUARTER_PAST_ONE, "2026-01-01T01:15:00+00:00")]
    )
    await coordinator.async_refresh()
    reading = coordinator.data[SINCE_QUARTER_PAST_ONE]
    # On from a quarter past one to half past, then the whole third hour.
    assert reading.value == 1.25
    assert reading.estimated is False
    assert reading.period_start == (T0 + timedelta(hours=1, minutes=15)).timestamp()
    assert reading.period_end == (T0 + timedelta(hours=3)).timestamp()


async def test_a_part_hour_is_estimated_once_its_history_is_gone(recorder, freezer):
    hass = recorder
    coordinator = await compiled_entry(
        hass,
        freezer,
        [
            custom(SINCE_QUARTER_PAST_ONE, "2026-01-01T01:15:00+00:00"),
            custom(LAST_HOUR_COUNT, "2026-01-01T01:15:00+00:00", metric="count"),
        ],
    )
    # The recorder now holds nothing before two o'clock.
    with patch.object(
        Compiler,
        "async_earliest_state_ts",
        return_value=(T0 + timedelta(hours=2)).timestamp(),
    ):
        await coordinator.async_refresh()
    reading = coordinator.data[SINCE_QUARTER_PAST_ONE]
    # Three quarters of the second hour's half hour on, then the third hour.
    assert reading.value == 1.38
    assert reading.estimated is True
    # The second hour's one change, three quarters of the hour in: whole.
    assert coordinator.data[LAST_HOUR_COUNT].value == 2


async def test_hour_timelines_are_read_once_and_dropped_by_a_compile(recorder, freezer):
    hass = recorder
    coordinator = await compiled_entry(
        hass,
        freezer,
        [
            custom(SINCE_QUARTER_PAST_ONE, "2026-01-01T01:15:00+00:00"),
            custom("sensor.since_quarter_to_two", "2026-01-01T01:45:00+00:00"),
        ],
    )
    with patch.object(
        Compiler, "async_tail", wraps=coordinator._compiler.async_tail
    ) as tail:
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        # One hour, two sensors, two refreshes: read once. No live tail is
        # read either - both windows end at the watermark.
        assert tail.call_count == 1
        async_dispatcher_send(
            hass,
            compiled_signal(ENTITY),
            (T0 + timedelta(hours=1)).timestamp(),
            (T0 + timedelta(hours=3)).timestamp(),
        )
        await hass.async_block_till_done()
        assert tail.call_count == 2


async def test_a_compile_of_the_trailing_window_keeps_older_sums(recorder, freezer):
    hass = recorder
    coordinator = await compiled_entry(
        hass,
        freezer,
        [sensor(YESTERDAY, ["on"], period="yesterday"), sensor(ON_TODAY, ["on"])],
    )
    with patch(
        "custom_components.discrete_statistics.coordinator.rows.sums_at",
        wraps=rows_module.sums_at,
    ) as sums_at:
        await coordinator.async_refresh()
        # Yesterday's two edges and today's end, the watermark.
        assert sums_at.call_count == 3
        # The hourly compile rewrites the trailing hours: only the edge
        # after its start is read again.
        async_dispatcher_send(
            hass,
            compiled_signal(ENTITY),
            (T0 + timedelta(hours=1)).timestamp(),
            (T0 + timedelta(hours=3)).timestamp(),
        )
        await hass.async_block_till_done()
        assert sums_at.call_count == 4
        # A recompute of everything: every edge.
        async_dispatcher_send(
            hass,
            compiled_signal(ENTITY),
            (T0 - timedelta(days=2)).timestamp(),
            (T0 + timedelta(hours=3)).timestamp(),
        )
        await hass.async_block_till_done()
        assert sums_at.call_count == 7


async def test_a_finished_window_reads_no_tail(recorder, freezer):
    hass = recorder
    coordinator = await compiled_entry(
        hass, freezer, [sensor(YESTERDAY, ["on"], period="yesterday")]
    )
    with patch.object(
        Compiler, "async_tail", wraps=coordinator._compiler.async_tail
    ) as tail:
        await coordinator.async_refresh()
        assert tail.call_count == 0
        assert coordinator.data[YESTERDAY].value == 0.0


async def test_a_custom_window_from_a_start_and_a_duration(recorder, freezer):
    hass = recorder
    coordinator = await compiled_entry(
        hass,
        freezer,
        [
            custom(
                "sensor.one_to_two",
                "{{ '2026-01-01T01:00:00+00:00' }}",
                duration=3600.0,
            )
        ],
    )
    await coordinator.async_refresh()
    reading = coordinator.data["sensor.one_to_two"]
    assert reading.value == 0.5
    assert reading.period_end == (T0 + timedelta(hours=2)).timestamp()


async def test_a_template_that_does_not_render_makes_the_sensor_unavailable(
    recorder, freezer
):
    hass = recorder
    coordinator = await compiled_entry(
        hass,
        freezer,
        [
            custom("sensor.broken", "{{ nonsense( }}"),
            custom("sensor.not_a_time", "{{ 'soon' }}"),
            custom("sensor.one_of_three", None, None, 3600.0),
            sensor(ON_TODAY, ["on"]),
        ],
    )
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    for subentry_id in ("sensor.broken", "sensor.not_a_time", "sensor.one_of_three"):
        reading = coordinator.data[subentry_id]
        assert reading.value is None
        assert reading.reason is not None and reading.reason.startswith("template")
    # The others on the entry are unaffected.
    assert coordinator.data[ON_TODAY].value == 1.5


def test_render_datetime_takes_a_datetime_string_or_a_timestamp(hass):
    assert (
        render_datetime(hass, "2026-01-01T01:15:00+00:00")
        == (T0 + timedelta(hours=1, minutes=15)).timestamp()
    )
    # A timestamp, as `as_timestamp(now())` renders one.
    assert render_datetime(hass, "{{ 1767225600.0 }}") == 1767225600.0
    with pytest.raises(ValueError):
        render_datetime(hass, "{{ 'soon' }}")
    with pytest.raises(ValueError):
        render_datetime(hass, "{{ nonsense( }}")
