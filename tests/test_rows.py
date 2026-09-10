"""The read-only recorder queries the sensors and the card share."""

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.discrete_statistics import rows
from custom_components.discrete_statistics.const import METRIC_DURATION
from custom_components.discrete_statistics.payload import metadata_for

ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override the root conftest fixture; see tests/test_compiler.py."""
    yield


@pytest.fixture
async def recorder(recorder_mock, hass):
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


async def seed(hass, statistic_id, start, sums):
    async_add_external_statistics(
        hass,
        metadata_for(METRIC_DURATION, statistic_id, "Grid Status: On (h)"),
        [
            {"start": start + timedelta(hours=i), "sum": value}
            for i, value in enumerate(sums)
            if value is not None
        ],
    )
    # `Recorder.async_block_till_done` returns immediately when the queue is
    # empty, and it is empty from the moment the recorder thread picks the
    # import up - before it has committed it. `async_wait_recording_done`
    # waits on a task queued behind the import, so the rows are there.
    await async_wait_recording_done(hass)


async def sums_at(hass, ids, edge):
    return await get_instance(hass).async_add_executor_job(
        rows.sums_at, hass, ids, edge.timestamp()
    )


async def test_sums_at_reads_the_row_before_the_edge(recorder):
    await seed(recorder, ON, T0, [0.5, 1.0, 1.5])
    await seed(recorder, OFF, T0, [0.5, 1.0, 1.5])
    # The row starting the hour before the edge holds the sum at the edge.
    assert await sums_at(recorder, {ON, OFF}, T0 + timedelta(hours=2)) == {
        ON: 1.0,
        OFF: 1.0,
    }


async def test_sums_at_carries_across_a_hole(recorder):
    await seed(recorder, ON, T0, [0.5, 1.0, None, None, 2.0])
    assert await sums_at(recorder, {ON}, T0 + timedelta(hours=4)) == {ON: 1.0}


async def test_sums_at_is_zero_before_the_series_and_absent_when_unknown(recorder):
    await seed(recorder, ON, T0, [0.5])
    assert await sums_at(recorder, {ON, OFF}, T0) == {ON: 0.0}


async def test_series_start_is_the_earliest_row_across_statistics(recorder):
    await seed(recorder, ON, T0 + timedelta(hours=2), [0.5])
    await seed(recorder, OFF, T0, [0.5])
    start = await get_instance(recorder).async_add_executor_job(
        rows.series_start, recorder, {ON, OFF}
    )
    assert start == T0.timestamp()
    assert (
        await get_instance(recorder).async_add_executor_job(
            rows.series_start, recorder, {"discrete_statistics:nothing_on_duration"}
        )
        is None
    )
