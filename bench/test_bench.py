"""The pytest driver for the bench harness.

One "test" per mode, all of them skipped but the one `run.json` names.
The work is in `harness.py`; this module only builds the `hass` the
harness measures through and hands the results to `write_results`.

`BENCH_RUN` names the run file `script/bench` writes immediately before
the run - mode, engine, repeat count, and where the cases, the data and
the results live. Everything else comes from the cases document.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from sqlalchemy import text as sql_text

from custom_components.discrete_statistics.const import DOMAIN

from . import cases as cases_module
from . import harness

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

RUN_PATH = Path(os.environ.get("BENCH_RUN", "bench/results/run.json"))
RUN = harness.load_run(RUN_PATH) if RUN_PATH.exists() else harness.Run()
CASES = cases_module.load(RUN.cases)
SCHEMA_VERSION = 53


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override the root conftest fixture; `recorder_db_url` must come first."""
    yield


@pytest.fixture
async def recorder(hass, recorder_mock):
    # The test recorder engine is created with echo on, and logging every
    # statement to stdout would be most of what we then time.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    await hass.config.async_set_time_zone(CASES.time_zone)
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


@pytest.mark.skipif(RUN.mode != "measure", reason="bench mode")
async def test_measure(recorder, recorder_db_url, hass_ws_client):
    hass = recorder
    # Our websocket command is registered by the component's setup.
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    now = await harness.anchor(hass)
    assert now is not None, "no statistics of ours in this database"
    client = (
        await hass_ws_client(hass)
        if any(case.websocket for case in CASES.buckets)
        else None
    )
    bench = await harness.measure(
        hass, CASES, RUN, now, dt_util.get_default_time_zone(), client
    )
    await harness.write_results(bench, hass, RUN, recorder_db_url)


@pytest.mark.skipif(RUN.mode != "build", reason="bench mode")
async def test_build(recorder):
    """Compile every entry from its earliest retained state. Writes."""
    await harness.build(recorder, RUN, CASES)


@pytest.mark.skipif(RUN.mode != "compile", reason="bench mode")
async def test_compile(recorder, recorder_db_url):
    """The write path: a trailing-window compile, and one day recompiled. Writes."""
    hass = recorder
    end = await harness.anchor(hass)
    assert end is not None, "no statistics of ours in this database"
    bench = await harness.compile_(hass, RUN, end, CASES)
    await harness.write_results(bench, hass, RUN, recorder_db_url)


@pytest.mark.skipif(RUN.mode != "schema", reason="bench mode")
async def test_schema(recorder):
    """Create Home Assistant's own schema in an empty database.

    Booting the recorder against an empty database is what creates it,
    DDL and `schema_changes` row alike; `conftest.py`'s `recorder_db_url`
    override is what stops the plugin dropping it again on the way out.
    """
    hass = recorder

    def introspect():
        engine = get_instance(hass).engine
        with engine.connect() as conn:
            return (
                conn.execute(
                    sql_text("SELECT MAX(schema_version) FROM schema_changes")
                ).scalar(),
                sorted(engine.dialect.get_table_names(conn)),
            )

    version, tables = await get_instance(hass).async_add_executor_job(introspect)
    print(
        f"\nschema_version {version}; {len(tables)} tables: {', '.join(tables)}",
        flush=True,
    )
    assert version == SCHEMA_VERSION, f"expected schema {SCHEMA_VERSION}, got {version}"
