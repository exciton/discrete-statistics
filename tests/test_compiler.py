"""Tests for the compiler against a real recorder."""

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.setup import async_setup_component

from homeassistant.components.recorder.statistics import async_add_external_statistics

from custom_components.discrete_statistics import compiler as compiler_module
from custom_components.discrete_statistics.compiler import TRAILING_HOURS, Compiler
from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import HOUR, METRIC_COUNT, METRIC_DURATION
from custom_components.discrete_statistics.payload import metadata_for
from custom_components.discrete_statistics.registry import Registry

ENTITY = "binary_sensor.grid_status"
DURATION_OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"
COUNT_OFF = "discrete_statistics:binary_sensor_grid_status_off_count"
DURATION_ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
COUNT_ON = "discrete_statistics:binary_sensor_grid_status_on_count"
DURATION_NO_DATA = "discrete_statistics:binary_sensor_grid_status_no_data_duration"
COUNT_NO_DATA = "discrete_statistics:binary_sensor_grid_status_no_data_count"


def cfg():
    return EntityConfig(
        entity_id=ENTITY, name="Grid Status", default="record_known", states={}
    )


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
    """A hass with the recorder set up."""
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


async def read_sums(hass, statistic_id, start, end):
    """Return the cumulative sums recorded for a statistic."""
    # async_add_external_statistics only queues the write, so drain the
    # recorder before querying it back.
    await get_instance(hass).async_block_till_done()
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return [row["sum"] for row in result.get(statistic_id, [])]


async def test_compiles_nothing_without_history(recorder):
    hass = recorder
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    assert await compiler.async_compile_incremental(cfg()) == 0


async def test_registers_statistic_ids_it_writes(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    assert DURATION_ON in registry.statistic_ids_for(ENTITY)
    assert registry.describe(DURATION_ON) == (ENTITY, "on", "duration")


async def test_records_an_outage_duration_and_count(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(minutes=45))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    sums = await read_sums(
        hass, DURATION_OFF, start, start + timedelta(hours=2)
    )
    assert sums[0] == pytest.approx(0.25)
    counts = await read_sums(
        hass, COUNT_OFF, start, start + timedelta(hours=2)
    )
    assert counts[0] == 1


async def test_compiling_twice_is_idempotent(recorder, freezer):
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    await compiler.async_compile(cfg(), start.timestamp())
    first = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=2))
    await compiler.async_compile(cfg(), start.timestamp())
    second = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=2))

    # "off" runs from 00:30 to the end of the window: 1800 s in the first
    # hour, a full hour in the second. Assert the values, not just that two
    # reads agree - they would agree if both were empty.
    assert first == [0.5, 1.5]
    assert first == second


async def test_cadence_invariance(recorder, freezer):
    """Compiling in advancing steps must equal compiling all at once."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    for offset, state in ((70, "off"), (100, "on"), (200, "off")):
        freezer.move_to(start + timedelta(minutes=offset))
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    freezer.move_to(start + timedelta(hours=5))
    await compiler.async_compile(cfg(), start.timestamp())
    all_at_once = await read_sums(
        hass, DURATION_OFF, start, start + timedelta(hours=5)
    )
    assert all_at_once  # the comparison below is worthless if this is empty

    # Recompute the same range as a sliding window, the way incremental
    # compilation actually runs: each step ends an hour later than the last
    # and starts a trailing window back from its own end. Only the final
    # step's hours are rewritten by that step, so the earlier hours in the
    # result below are the ones the earlier steps wrote, and every step but
    # the first reads a non-empty base_sums.
    for hour in range(1, 6):
        step_end = start + timedelta(hours=hour)
        step_start = max(
            start.timestamp(), step_end.timestamp() - TRAILING_HOURS * HOUR
        )
        await compiler.async_compile(cfg(), step_start, step_end.timestamp())
    stepwise = await read_sums(
        hass, DURATION_OFF, start, start + timedelta(hours=5)
    )

    assert all_at_once == stepwise


async def test_a_state_absent_from_a_window_keeps_its_cumulative_base(
    recorder, freezer
):
    """A statistic missing from a window must not restart its sum at zero."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    # "off" occurs in the first window and the third, but not the second.
    for offset, state in (
        (timedelta(minutes=30), "off"),
        (timedelta(hours=1), "on"),
        (timedelta(hours=4, minutes=15), "off"),
        (timedelta(hours=4, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    for window in range(3):
        window_start = start + timedelta(hours=2 * window)
        await compiler.async_compile(
            cfg(),
            window_start.timestamp(),
            (window_start + timedelta(hours=2)).timestamp(),
        )

    sums = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=6))

    assert len(sums) == 6
    # 1800 s in the first window, nothing in the second, 900 s in the third.
    assert sums == sorted(sums), f"cumulative sum went backwards: {sums}"
    assert sums[-1] == pytest.approx(0.75)


async def test_back_to_back_compiles_see_the_previous_write(recorder, freezer):
    """Adjacent windows compiled in succession must carry the base forward.

    Deliberately without draining the recorder between the two calls: the
    compiler drains its own writes before returning, so a caller compiling
    one window after another does not have to know about the queue.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    for offset, state in (
        (timedelta(minutes=30), "off"),
        (timedelta(hours=1), "on"),
        (timedelta(hours=2, minutes=30), "off"),
        (timedelta(hours=2, minutes=45), "on"),
        (timedelta(hours=4, minutes=15), "off"),
        (timedelta(hours=4, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    for window in range(3):
        window_start = start + timedelta(hours=2 * window)
        await compiler.async_compile(
            cfg(),
            window_start.timestamp(),
            (window_start + timedelta(hours=2)).timestamp(),
        )

    sums = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=6))

    # 1800 s of "off" in the first window, then 900 s in each of the next two.
    assert sums == sorted(sums), f"cumulative sum went backwards: {sums}"
    assert sums == [0.5, 0.5, 0.75, 0.75, 1.0, 1.0]


async def test_watermark_is_the_newest_hour_across_statistics(recorder):
    """The watermark must not follow the alphabetically first statistic."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    registry = Registry(hass)
    await registry.async_load()
    # COUNT_OFF sorts before DURATION_ON, and is the one left behind.
    await registry.async_register(
        ENTITY,
        {COUNT_OFF: ("off", METRIC_COUNT), DURATION_ON: ("on", METRIC_DURATION)},
    )
    assert registry.statistic_ids_for(ENTITY)[0] == COUNT_OFF

    for statistic_id, state, metric, hours in (
        (COUNT_OFF, "off", METRIC_COUNT, 2),
        (DURATION_ON, "on", METRIC_DURATION, 4),
    ):
        async_add_external_statistics(
            hass,
            metadata_for(cfg(), state, metric, statistic_id),
            [
                {"start": start + timedelta(hours=hour), "sum": float(hour)}
                for hour in range(hours)
            ],
        )
    await get_instance(hass).async_block_till_done()

    compiler = Compiler(hass, registry)
    watermark = await compiler._async_watermark(ENTITY)

    assert watermark == (start + timedelta(hours=3)).timestamp()


async def test_recompiling_back_past_known_history_is_no_data(recorder, freezer):
    """Reaching back before an established series still yields no_data.

    The leading no_data is trimmed only on an entity's first compile. Once
    statistics exist, a recompute asked to start earlier than the entity's
    history has a real gap to describe, and trimming it would leave a hole
    the next run would read as a missing cumulative base.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    # First compile: establishes the series, and emits no no_data at all.
    await compiler.async_compile(cfg(), start.timestamp())
    assert DURATION_NO_DATA not in registry.statistic_ids_for(ENTITY)

    # Second compile, reaching back before the first known state.
    await compiler.async_compile(cfg(), start.timestamp())

    sums = await read_sums(hass, DURATION_NO_DATA, start, start + timedelta(hours=4))
    assert sums[-1] == pytest.approx(2.0)


async def test_an_ignored_state_at_the_window_start_does_not_destroy_durations(
    recorder, freezer
):
    """A trailing compile across an ignored stretch must agree with a full one.

    `include_start_time_state` returns exactly one row before the boundary.
    When that row is `unavailable`, the recordable state one row further back
    is invisible and the whole span would be attributed to no_data - and then
    written over the correct rows, because the upsert is idempotent and the
    window start moves every run.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for offset, state in (
        (timedelta(0), "on"),
        (timedelta(minutes=30), "unavailable"),
        (timedelta(hours=2, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    await compiler.async_compile(cfg(), start.timestamp())
    full = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    # `unavailable` carries "on" forward, so the entity is "on" throughout.
    assert full == [1.0, 2.0, 3.0, 4.0]

    # Now the trailing window the hourly run would use, whose start lands in
    # the middle of the ignored stretch.
    await compiler.async_compile(
        cfg(), (start + timedelta(hours=1)).timestamp()
    )
    trailing = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))

    assert trailing == full

    no_data = await read_sums(
        hass, DURATION_NO_DATA, start, start + timedelta(hours=4)
    )
    assert no_data in ([], [0.0, 0.0, 0.0, 0.0]), no_data


async def test_a_boundary_transition_is_counted_once_from_either_window(
    recorder, freezer
):
    """A transition exactly on an hour must not depend on the window start."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    for offset, state in (
        (timedelta(0), "on"),
        (timedelta(hours=2), "off"),
        (timedelta(hours=2, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    await compiler.async_compile(cfg(), start.timestamp())
    from_earlier = await read_sums(
        hass, COUNT_OFF, start, start + timedelta(hours=4)
    )
    assert from_earlier == [0, 0, 1, 1]

    # Recompile a window that begins exactly on the transition.
    await compiler.async_compile(
        cfg(), (start + timedelta(hours=2)).timestamp()
    )
    from_boundary = await read_sums(
        hass, COUNT_OFF, start, start + timedelta(hours=4)
    )

    assert from_boundary == from_earlier

    # Durations are untouched by the boundary event.
    seconds = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=4))
    assert seconds == [0.0, 0.0, 0.5, 0.5]


async def test_no_data_has_no_count_statistic(recorder, freezer):
    """no_data has no transitions into it, so a count would be a fixed zero."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    # Twice: the first compile establishes the series, and only then does a
    # start before the entity's history describe a gap rather than the
    # trimmed opening sliver.
    await compiler.async_compile(cfg(), start.timestamp())
    await compiler.async_compile(cfg(), start.timestamp())

    # The gap is real: the duration statistic exists and holds two hours.
    seconds = await read_sums(
        hass, DURATION_NO_DATA, start, start + timedelta(hours=4)
    )
    assert seconds[-1] == pytest.approx(2.0)

    assert COUNT_NO_DATA not in registry.statistic_ids_for(ENTITY)
    assert COUNT_ON in registry.statistic_ids_for(ENTITY)
    assert (
        await read_sums(hass, COUNT_NO_DATA, start, start + timedelta(hours=4))
        == []
    )


async def test_compiling_across_a_chunk_boundary(recorder, freezer, monkeypatch):
    """A window longer than CHUNK_HOURS must carry sums across the seam."""
    monkeypatch.setattr(compiler_module, "CHUNK_HOURS", 2)

    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    # Transitions on both sides of every 2-hour chunk seam.
    for offset, state in (
        (timedelta(minutes=30), "off"),
        (timedelta(hours=1, minutes=45), "on"),
        (timedelta(hours=2, minutes=15), "off"),
        (timedelta(hours=3, minutes=45), "on"),
        (timedelta(hours=4, minutes=30), "off"),
        (timedelta(hours=5, minutes=30), "on"),
    ):
        freezer.move_to(start + offset)
        hass.states.async_set(ENTITY, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=6))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    assert await compiler.async_compile(cfg(), start.timestamp()) == 6

    end = start + timedelta(hours=6)
    duration_ids = [
        statistic_id
        for statistic_id in registry.statistic_ids_for(ENTITY)
        if registry.describe(statistic_id)[2] == METRIC_DURATION
    ]
    assert len(duration_ids) >= 2, duration_ids

    total = 0.0
    for statistic_id in duration_ids:
        sums = await read_sums(hass, statistic_id, start, end)
        assert len(sums) == 6, (statistic_id, sums)
        assert sums == sorted(sums), f"{statistic_id} went backwards: {sums}"
        total += sums[-1]

    # Time is conserved across the seam: every second of the six hours is
    # attributed to exactly one state.
    assert total == pytest.approx(6.0)

    off_duration = await read_sums(hass, DURATION_OFF, start, end)
    # off runs 0:30-1:45, 2:15-3:45 and 4:30-5:30.
    assert off_duration[-1] == pytest.approx((4500 + 5400 + 3600) / 3600)


async def read_day(hass, statistic_id, start, end):
    """Return the day-period rollup rows for a statistic."""
    await get_instance(hass).async_block_till_done()
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "day",
        None,
        {"mean", "min", "max", "sum"},
    )
    return result.get(statistic_id, [])


async def test_hourly_values_roll_up_into_a_daily_mean_min_and_max(recorder, freezer):
    """The point of writing mean/min/max: second-order statistics for free.

    The recorder reduces the hourly rows itself, so a statistics-graph card
    asking for `mean` over a day answers "average hours on per hour" and
    `max` answers "the busiest hour" - neither of which the cumulative sum
    can express.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(hours=3, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    # Hourly "on" durations are 1.0, 1.0, 0.0, 0.5.
    hourly = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=4))
    assert hourly == [1.0, 2.0, 2.0, 2.5]

    rows = await read_day(
        hass, DURATION_ON, start - timedelta(days=1), start + timedelta(days=2)
    )
    assert len(rows) == 1
    assert rows[0]["mean"] == pytest.approx(2.5 / 4)
    assert rows[0]["min"] == 0.0
    assert rows[0]["max"] == 1.0
    # The sum is untouched by the reduction: it stays the cumulative total.
    assert rows[0]["sum"] == pytest.approx(2.5)


async def test_a_new_entity_does_not_open_with_no_data(recorder, freezer):
    """The first compile starts at the first whole hour it knows.

    An entity's first state almost never lands on the hour, so compiling
    from the hour containing it would give every helper a permanent
    no_data statistic recording a few minutes of ignorance.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2, minutes=23))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=5))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    hours = await compiler.async_compile(cfg(), start.timestamp())

    assert DURATION_NO_DATA not in registry.statistic_ids_for(ENTITY)
    assert COUNT_NO_DATA not in registry.statistic_ids_for(ENTITY)
    # Hours 3 and 4 only: the partial hour 2 is dropped along with hours 0-1.
    assert hours == 2
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=5)) == [
        1.0,
        2.0,
    ]


async def test_a_first_state_on_the_hour_loses_nothing(recorder, freezer):
    """No hour is trimmed when the first state already sits on a boundary."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(hours=2))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=5))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    assert DURATION_NO_DATA not in registry.statistic_ids_for(ENTITY)
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=5)) == [
        1.0,
        2.0,
        3.0,
    ]


async def test_an_unknown_opening_state_is_trimmed_too(recorder, freezer):
    """`unknown` is the usual first state, and it is not a state we record.

    Trimming to the hour containing the first *row* would leave the gap in
    place; it has to be the first row the config actually resolves.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=10))
    hass.states.async_set(ENTITY, "unknown")
    await hass.async_block_till_done()

    freezer.move_to(start + timedelta(hours=2, minutes=30))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=5))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    assert DURATION_NO_DATA not in registry.statistic_ids_for(ENTITY)
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=5)) == [
        1.0,
        2.0,
    ]


async def test_the_incremental_first_run_is_trimmed(recorder, freezer):
    """The watermark-less path is how a helper actually gets its first run."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=17))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=1, minutes=30))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=3))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile_incremental(cfg())

    assert DURATION_NO_DATA not in registry.statistic_ids_for(ENTITY)
    # Hours 1 and 2, and they still tile the clock exactly.
    on = await read_sums(hass, DURATION_ON, start, start + timedelta(hours=3))
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=3))
    assert on == [0.5, 0.5]
    assert off == [0.5, 1.5]


async def test_nothing_is_compiled_until_a_whole_hour_is_known(recorder, freezer):
    """A helper created mid-hour waits for the next one rather than inventing.

    Hour 0 has completed, so there is an hour to compile - but it is only
    known from 00:20, and the first hour known end to end has not finished
    yet. Compiling hour 0 anyway is what used to manufacture the opening
    no_data.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    freezer.move_to(start + timedelta(minutes=20))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()

    freezer.move_to(start + timedelta(hours=1, minutes=30))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)

    assert await compiler.async_compile_incremental(cfg()) == 0
    assert registry.statistic_ids_for(ENTITY) == []


async def _seed_two_states(hass, freezer, start):
    """on for hour 0, off from hour 1 onward. No trim: the first state is on the hour."""
    freezer.move_to(start)
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    freezer.move_to(start + timedelta(hours=1))
    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()


async def _delete(hass, statistic_ids):
    get_instance(hass).async_clear_statistics(list(statistic_ids))
    await get_instance(hass).async_block_till_done()


async def test_a_deleted_statistic_is_forgotten_not_recreated(recorder, freezer):
    """Settings -> System -> Tools -> Statistics is the whole interface.

    Without this the registry still names the statistic, so it stays in
    known_states and the very next compile writes it back - densely, and
    forever.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())
    assert DURATION_ON in registry.statistic_ids_for(ENTITY)

    # "on" never occurs again, so only the registry could resurrect it.
    await _delete(hass, [DURATION_ON, COUNT_ON])

    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile_incremental(cfg())

    assert DURATION_ON not in registry.statistic_ids_for(ENTITY)
    assert COUNT_ON not in registry.statistic_ids_for(ENTITY)
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=6)) == []
    # The surviving state carries on undisturbed, still dense and monotonic.
    off = await read_sums(hass, DURATION_OFF, start, start + timedelta(hours=6))
    assert off == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


async def test_the_forgotten_statistic_stays_forgotten_across_a_reload(
    recorder, freezer
):
    """The removal is persisted, not just dropped from memory."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    await _delete(hass, [DURATION_ON, COUNT_ON])
    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile_incremental(cfg())

    reloaded = Registry(hass)
    await reloaded.async_load()
    assert DURATION_ON not in reloaded.statistic_ids_for(ENTITY)


async def test_a_state_returns_while_any_of_its_statistics_survives(recorder, freezer):
    """Density is per state, so half a state cannot be deleted.

    Deleting the duration but not the count leaves "on" a known state, and
    the density invariant then requires a duration row for it in every hour.
    Both of a state's statistics have to go for the state to go.
    """
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())

    await _delete(hass, [DURATION_ON])

    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile_incremental(cfg())

    assert DURATION_ON in registry.statistic_ids_for(ENTITY)
    # Rebuilt from the trailing window only, so its history does not return.
    assert await read_sums(hass, DURATION_ON, start, start + timedelta(hours=6)) != []


async def test_nothing_is_forgotten_while_its_statistic_exists(recorder, freezer):
    """The reaping must not fire on a healthy entity."""
    hass = recorder
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    await _seed_two_states(hass, freezer, start)

    freezer.move_to(start + timedelta(hours=4))
    registry = Registry(hass)
    await registry.async_load()
    compiler = Compiler(hass, registry)
    await compiler.async_compile(cfg(), start.timestamp())
    before = registry.statistic_ids_for(ENTITY)
    assert before

    freezer.move_to(start + timedelta(hours=6))
    await compiler.async_compile_incremental(cfg())

    assert registry.statistic_ids_for(ENTITY) == before
