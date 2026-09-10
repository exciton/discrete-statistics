"""The compiler's read path handed out as a timeline, never written."""

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.setup import async_setup_component

from custom_components.discrete_statistics.bucketer import tally
from custom_components.discrete_statistics.compiler import (
    Compiler,
    Timeline,
    compiled_signal,
)
from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import HOUR
from tests.test_compiler import existing, read_sums

ENTITY = "binary_sensor.grid_status"
ON_DURATION = "discrete_statistics:binary_sensor_grid_status_on_duration"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def cfg(default="record_known", min_duration=0.0):
    return EntityConfig(
        entity_id=ENTITY,
        name="Grid Status",
        default=default,
        states={},
        min_duration=min_duration,
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override the root conftest fixture; see tests/test_compiler.py."""
    yield


@pytest.fixture
async def recorder(recorder_mock, hass):
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


async def play(hass, freezer, timeline, entity_id=ENTITY):
    """Set each (datetime, state) in turn and let the recorder commit it."""
    for when, state in timeline:
        freezer.move_to(when)
        hass.states.async_set(entity_id, state)
        await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()


async def test_tail_is_the_timeline_from_the_watermark(recorder, freezer):
    hass = recorder
    await play(
        hass,
        freezer,
        [
            (T0 - timedelta(hours=1), "off"),
            (T0 + timedelta(minutes=10), "on"),
            (T0 + timedelta(minutes=30), "off"),
        ],
    )
    freezer.move_to(T0 + timedelta(minutes=45))
    tail = await Compiler(hass).async_tail(
        cfg(), T0.timestamp(), (T0 + timedelta(minutes=45)).timestamp()
    )
    assert tail == Timeline(
        T0.timestamp(),
        "off",
        [
            ((T0 + timedelta(minutes=10)).timestamp(), "on"),
            ((T0 + timedelta(minutes=30)).timestamp(), "off"),
        ],
    )


async def test_tail_agrees_with_the_next_compile(recorder, freezer):
    """What the tail says an hour will hold is what the compile then writes."""
    hass = recorder
    await play(
        hass,
        freezer,
        [
            (T0 - timedelta(hours=1), "off"),
            (T0 + timedelta(minutes=15), "on"),
            (T0 + timedelta(minutes=45), "off"),
        ],
    )
    freezer.move_to(T0 + timedelta(minutes=50))
    compiler = Compiler(hass)
    tail = await compiler.async_tail(cfg(), T0.timestamp(), T0.timestamp() + HOUR)
    assert tail is not None
    freezer.move_to(T0 + timedelta(hours=1, minutes=5))
    await compiler.async_compile(cfg(), T0.timestamp())
    assert await read_sums(hass, ON_DURATION, T0, T0 + timedelta(hours=1)) == [
        pytest.approx(0.5)
    ]
    # Same rows, same carry, same transitions: half an hour on.
    assert tally(tail.carried, tail.transitions, T0.timestamp(), T0.timestamp() + HOUR)[
        "on"
    ] == (pytest.approx(1800.0), 1)


async def test_tail_writes_nothing(recorder, freezer):
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=1), "off"), (T0, "on")])
    freezer.move_to(T0 + timedelta(minutes=30))
    await Compiler(hass).async_tail(
        cfg(), T0.timestamp(), (T0 + timedelta(minutes=30)).timestamp()
    )
    assert await existing(hass) == []


async def test_tail_of_an_empty_window_is_none(recorder, freezer):
    hass = recorder
    await play(hass, freezer, [(T0 - timedelta(hours=1), "off")])
    assert (
        await Compiler(hass).async_tail(cfg(), T0.timestamp(), T0.timestamp()) is None
    )


async def test_an_open_short_spell_is_provisional(recorder, freezer):
    """`ignore_short` drops a spell still open at `now` until it has lasted."""
    hass = recorder
    config = cfg(default="ignore_short", min_duration=600)
    await play(
        hass,
        freezer,
        [(T0 - timedelta(hours=1), "off"), (T0 + timedelta(minutes=5), "on")],
    )
    freezer.move_to(T0 + timedelta(minutes=10))
    tail = await Compiler(hass).async_tail(
        config, T0.timestamp(), (T0 + timedelta(minutes=10)).timestamp()
    )
    assert tail == Timeline(T0.timestamp(), "off", [])
    freezer.move_to(T0 + timedelta(minutes=20))
    tail = await Compiler(hass).async_tail(
        config, T0.timestamp(), (T0 + timedelta(minutes=20)).timestamp()
    )
    assert tail == Timeline(
        T0.timestamp(), "off", [((T0 + timedelta(minutes=5)).timestamp(), "on")]
    )


async def test_compile_signals_when_done(recorder, freezer):
    hass = recorder
    heard = []
    async_dispatcher_connect(hass, compiled_signal(ENTITY), lambda: heard.append(1))
    compiler = Compiler(hass)
    # Nothing to compile: no signal.
    await compiler.async_compile(cfg(), T0.timestamp())
    await hass.async_block_till_done()
    assert heard == []
    await play(hass, freezer, [(T0, "on"), (T0 + timedelta(hours=1), "off")])
    freezer.move_to(T0 + timedelta(hours=2))
    await compiler.async_compile(cfg(), T0.timestamp())
    await hass.async_block_till_done()
    assert heard == [1]


async def test_async_compiled_reports_the_watermark(recorder, freezer):
    hass = recorder
    compiler = Compiler(hass)
    assert await compiler.async_compiled(ENTITY) == ({}, None)
    await play(hass, freezer, [(T0, "on"), (T0 + timedelta(hours=1), "off")])
    freezer.move_to(T0 + timedelta(hours=2))
    await compiler.async_compile(cfg(), T0.timestamp())
    existing, watermark = await compiler.async_compiled(ENTITY)
    assert ON_DURATION in existing
    assert watermark == (T0 + timedelta(hours=1)).timestamp()
