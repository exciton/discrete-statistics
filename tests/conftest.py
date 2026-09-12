"""Fixtures and helpers shared by the tests.

`pytest_plugins` stays in the root `conftest.py`; declaring it here is an
error in modern pytest and breaks the whole suite.
"""

import functools as ft
import re
from datetime import timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from sqlalchemy import event as sqlalchemy_event

from custom_components.discrete_statistics.const import METRIC_DURATION
from custom_components.discrete_statistics.coordinator import REFRESH_COOLDOWN
from custom_components.discrete_statistics.statistic_ids import belongs_to, parse

ENTITY = "binary_sensor.grid_status"


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


@pytest.fixture
async def recorder_utc(recorder):
    """The same, in UTC, for tests that spell their hours out."""
    await recorder.config.async_set_time_zone("UTC")
    await recorder.async_block_till_done()
    return recorder


async def play(hass, freezer, timeline, entity_id=ENTITY):
    """Set each (datetime, state) in turn and let the recorder commit it."""
    for when, state in timeline:
        freezer.move_to(when)
        hass.states.async_set(entity_id, state)
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


async def changed_state(hass, freezer, when, state, entity_id=ENTITY):
    """Set the entity's state at `when` and let the debounced refresh land."""
    freezer.move_to(when)
    hass.states.async_set(entity_id, state)
    await hass.async_block_till_done()
    await past_the_cooldown(hass, freezer, when)


async def existing(hass, entity_id=ENTITY):
    """The statistic IDs the recorder holds for an entity."""
    await get_instance(hass).async_block_till_done()
    metadata = await get_instance(hass).async_add_executor_job(
        ft.partial(get_metadata, hass, statistic_source="discrete_statistics")
    )
    return sorted(sid for sid in metadata if belongs_to(sid, entity_id))


async def read_sums(hass, statistic_id, start, end, entity_id=ENTITY):
    """The cumulative sum at each compiled hour in [start, end).

    A row stands only where something changed, so the sum at a quiet hour
    is carried from the newest row before it - zero before the statistic's
    first row - and an hour is compiled when any duration statistic of the
    entity holds a row there.
    """
    durations = [
        sid
        for sid in await existing(hass, entity_id)
        if parse(sid)[2] == METRIC_DURATION
    ]
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(days=400),
        end,
        {*durations, statistic_id},
        "hour",
        None,
        {"sum"},
    )
    compiled = sorted(
        {
            row["start"]
            for sid in durations
            for row in result.get(sid, [])
            if start.timestamp() <= row["start"] < end.timestamp()
        }
    )
    own = sorted((row["start"], row["sum"]) for row in result.get(statistic_id, []))
    sums, i, running = [], 0, 0.0
    for hour in compiled:
        while i < len(own) and own[i][0] <= hour:
            running = own[i][1]
            i += 1
        sums.append(running)
    return sums


@pytest.fixture
def statements(hass, recorder_mock):
    """The SELECTs the recorder's engine runs against the statistics table.

    `statistics_meta` is not one of them: `_` is a word character, so the
    boundary after `statistics` does not match it.
    """
    seen: list[str] = []

    def listen(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and re.search(
            r"\bstatistics\b", statement
        ):
            seen.append(statement)

    engine = get_instance(hass).engine
    sqlalchemy_event.listen(engine, "before_cursor_execute", listen)
    yield seen
    sqlalchemy_event.remove(engine, "before_cursor_execute", listen)


class Fetched:
    """How many rows the statistics-table SELECTs have handed back."""

    def __init__(self) -> None:
        self.rows = 0
        self.on = False

    def clear(self) -> None:
        self.rows = 0


@pytest.fixture
def fetched(hass, recorder_mock):
    """Rows returned by the same statements `statements` counts.

    Counted through the sqlite3 connection's `row_factory`, which sees
    every row a cursor hands back; the flag says whether the statement
    running is one of ours. Rows are what a leaner query saves when the
    statement count is already one.
    """
    counter = Fetched()

    def row(cursor, values):
        if counter.on:
            counter.rows += 1
        return values

    def factory(dbapi_connection, *_):
        dbapi_connection.row_factory = row

    def before(conn, cursor, statement, parameters, context, executemany):
        counter.on = statement.lstrip().upper().startswith("SELECT") and bool(
            re.search(r"\bstatistics\b", statement)
        )

    engine = get_instance(hass).engine
    sqlalchemy_event.listen(engine, "connect", factory)
    sqlalchemy_event.listen(engine, "checkout", factory)
    sqlalchemy_event.listen(engine, "before_cursor_execute", before)
    yield counter
    sqlalchemy_event.remove(engine, "connect", factory)
    sqlalchemy_event.remove(engine, "checkout", factory)
    sqlalchemy_event.remove(engine, "before_cursor_execute", before)
