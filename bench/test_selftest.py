"""The bench, measured against a database this test builds itself.

`script/bench selftest` runs this and nothing else. It is not part of
`script/test tests/`: it proves the *harness* works, not the integration.

Two cases over a series seeded one row per hour with a known change, so
every number the harness records can be checked by hand: the card's
buckets, the stock command's agreement with them, a rolling sensor's
value, and the meter's own counts - a meter that stopped counting
statements or rows fails here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.discrete_statistics.const import (
    METRIC_COUNT,
    METRIC_DURATION,
)
from custom_components.discrete_statistics.payload import metadata_for

from . import cases as cases_module
from . import harness

ENTITY = "binary_sensor.grid_status"
DURATION_ID = "discrete_statistics:binary_sensor_grid_status_on_duration"
COUNT_ID = "discrete_statistics:binary_sensor_grid_status_on_count"
T0 = datetime(2026, 1, 5, tzinfo=UTC)
HOURS = 48
# One row per hour, cumulative: half an hour on, one transition in.
PER_HOUR_DURATION = 0.5
PER_HOUR_COUNT = 1.0


@pytest.fixture
def recorder_db_url(tmp_path_factory, hass_fixture_setup):
    """A SQLite file of our own, left alone by the plugin's create/drop."""
    assert not hass_fixture_setup
    directory = tmp_path_factory.mktemp("bench-selftest")
    yield f"sqlite:///{directory}/home-assistant_v2.db"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override the root conftest fixture; `recorder_db_url` must come first."""
    yield


@pytest.fixture
async def seeded(hass, recorder_mock, tmp_path):
    """A recorder holding one entity's two statistics, and a data directory."""
    await hass.config.async_set_time_zone("UTC")
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    for statistic_id, metric, step in (
        (DURATION_ID, METRIC_DURATION, PER_HOUR_DURATION),
        (COUNT_ID, METRIC_COUNT, PER_HOUR_COUNT),
    ):
        async_add_external_statistics(
            hass,
            metadata_for(metric, statistic_id, f"Grid Status: On ({metric})"),
            [{"start": T0 + timedelta(hours=i), "sum": i * step} for i in range(HOURS)],
        )
    # `async_block_till_done` returns before the import has committed; this
    # waits on a task queued behind it.
    await async_wait_recording_done(hass)
    (tmp_path / "entries.json").write_text(
        '[{"data": {"entity_id": "' + ENTITY + '"},'
        ' "options": {"default": "record", "blank": "unknown", "name": null}}]'
    )
    return hass


def _cases():
    return cases_module.from_dict(
        {
            "time_zone": "UTC",
            "repeat": 2,
            "bucket_cases": [
                {
                    "name": "1 grid on duration / 1d / hour",
                    "entity": ENTITY,
                    "states": ["on"],
                    "metric": METRIC_DURATION,
                    "days": 1,
                    "period": "hour",
                }
            ],
            "sensors": [
                {
                    "name": "grid on duration last_24_hours",
                    "entity": ENTITY,
                    "states": ["on"],
                    "metric": METRIC_DURATION,
                    "period": "last_24_hours",
                    "live": False,
                }
            ],
        }
    )


async def test_measure_answers_and_counts(seeded, recorder_db_url, tmp_path):
    hass = seeded
    run = harness.Run(
        mode="measure",
        repeat=2,
        engine="sqlite",
        variant="selftest",
        branch="selftest",
        revision="0000000",
        cases="",
        data_dir=str(tmp_path),
        results_dir=str(tmp_path),
    )
    cases = _cases()
    now = await harness.anchor(hass)
    assert now == (T0 + timedelta(hours=HOURS)).timestamp()

    bench = await harness.measure(
        hass, cases, run, now, dt_util.get_default_time_zone()
    )
    by_case = {row["case"]: row for row in bench.results}
    assert sorted(by_case) == [
        "buckets 1 grid on duration / 1d / hour",
        f"frame   {ENTITY}",
        "sensor  grid on duration last_24_hours (2 edges)",
        "stock   1 grid on duration / 1d / hour",
    ]

    # The card's read: 24 hourly buckets, each the hour's own change.
    buckets = by_case["buckets 1 grid on duration / 1d / hour"]
    drawn = buckets["check"][DURATION_ID]
    assert len(drawn) == 24
    assert {round(change, 6) for _start, _end, change in drawn} == {PER_HOUR_DURATION}
    assert drawn[0][0] == (T0 + timedelta(hours=HOURS - 24)).timestamp() * 1000
    assert buckets["agreement"] == {"agree": 24, "disagree": 0, "extra_zero": 0}

    # The stock command, asked the same question, over the same rows.
    # The stock command's first row is the baseline its changes are
    # measured from, so 24 hourly changes come back as 24 rows.
    stock = by_case["stock   1 grid on duration / 1d / hour"]["check"][DURATION_ID]
    assert len(stock) == 24
    assert {round(change, 6) for _start, change in stock[1:]} == {PER_HOUR_DURATION}

    # A rolling window with `live` off is exactly its length, anchored on
    # the watermark: 24 hours at half an hour each.
    sensor = by_case["sensor  grid on duration last_24_hours (2 edges)"]["check"]
    assert sensor["value"] == pytest.approx(24 * PER_HOUR_DURATION)
    assert sensor["estimated"] is False

    frame = by_case[f"frame   {ENTITY}"]["check"]
    assert frame["statistics"] == [COUNT_ID, DURATION_ID]
    assert frame["watermark"] == (T0 + timedelta(hours=HOURS - 1)).timestamp()

    # The meter: every case issued statements against the statistics tables
    # and fetched rows. Zero here means the meter stopped counting.
    for name in ("buckets 1 grid on duration / 1d / hour", f"frame   {ENTITY}"):
        assert by_case[name]["statements"] > 0, name
        assert by_case[name]["statistics_statements"] > 0, name
        assert by_case[name]["rows"] > 0, name

    out = await harness.write_results(bench, hass, run, recorder_db_url)
    written = json.loads(out.read_text())
    assert written["branch"] == "selftest"
    assert written["our_statistics_rows"] == 2 * HOURS
    assert [row["case"] for row in written["results"]] == [
        row["case"] for row in bench.results
    ]
