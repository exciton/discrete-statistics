"""The diagnostics download: what an "it isn't compiling" report carries."""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.helpers.json import json_dumps

from custom_components.discrete_statistics.compiler import Compiler
from custom_components.discrete_statistics.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.conftest import ENTITY, ON_TODAY, T0, seeded, sensor


async def test_the_download_reports_the_entry_end_to_end(recorder_utc, freezer):
    hass = recorder_utc
    entry = await seeded(hass, freezer, [sensor("on today", ["on"])])

    result = await async_get_config_entry_diagnostics(hass, entry)
    # What the endpoint does with it.
    assert json_dumps(result)

    assert result["entry"]["data"] == {"entity_id": ENTITY}
    assert result["config"]["entity_id"] == ENTITY
    assert result["config"]["default"] == "record_known"
    assert result["configured_by"] == "entry"
    assert (
        "discrete_statistics:binary_sensor_grid_status_on_duration"
        in result["statistics"]["ids"]
    )
    assert (
        result["statistics"]["watermark_end"] == (T0 + timedelta(hours=3)).isoformat()
    )
    assert (
        result["recorder"]["earliest_retained_state"]
        == (T0 - timedelta(hours=1)).isoformat()
    )
    assert result["recorder"]["current_state"] == "on"
    assert result["recorder"]["backlog_threshold"] > 0
    assert result["recorder"]["time_zone"] == "UTC"
    [sensor_report] = result["sensors"]
    assert sensor_report["entity_id"] == ON_TODAY
    assert sensor_report["state"] == "1.5"
    assert sensor_report["attributes"]["estimated"] is False
    assert result["yaml_entities"] == []


async def test_a_failing_recorder_read_is_reported_not_raised(recorder_utc, freezer):
    hass = recorder_utc
    entry = await seeded(hass, freezer, [sensor("on today", ["on"])])

    with patch.object(Compiler, "async_compiled", side_effect=RuntimeError("db gone")):
        result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["statistics"] == {"error": "RuntimeError: db gone"}
    # The rest of the report is still there.
    assert result["recorder"]["current_state"] == "on"
    assert result["sensors"][0]["state"] == "1.5"
