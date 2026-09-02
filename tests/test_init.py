"""Tests for setup and scheduling."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import DEFAULT_RECORD_KNOWN, DOMAIN
from custom_components.discrete_statistics.registry import Registry

ENTITY = "binary_sensor.grid_status"
OTHER_ENTITY = "binary_sensor.water_pump"

CONFIG = {DOMAIN: [{"entity_id": ENTITY, "name": "Grid Status"}]}


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
    await async_setup_component(hass, "recorder", {"recorder": {}})
    await hass.async_block_till_done()
    return hass


async def test_setup_stores_runtime_data(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data
    assert len(hass.data[DOMAIN]["yaml_configs"]) == 1
    assert hass.data[DOMAIN]["yaml_configs"][0].entity_id == ENTITY


async def test_all_configs_joins_yaml_and_entries(recorder):
    hass = recorder
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()
    data = hass.data[DOMAIN]

    assert [c.entity_id for c in data["all_configs"]()] == [ENTITY]

    data["entry_configs"]["entry-1"] = EntityConfig(
        entity_id=OTHER_ENTITY, name=None, default=DEFAULT_RECORD_KNOWN
    )
    assert [c.entity_id for c in data["all_configs"]()] == [ENTITY, OTHER_ENTITY]


async def test_hourly_schedule_triggers_a_compile(recorder, freezer):
    hass = recorder
    freezer.move_to(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ) as compile_mock:
        freezer.move_to(datetime(2026, 1, 1, 11, 3, 0, tzinfo=timezone.utc))
        async_fire_time_changed(hass, datetime(2026, 1, 1, 11, 3, 0, tzinfo=timezone.utc))
        await hass.async_block_till_done()

    assert compile_mock.called


async def test_backlog_gate_skips_the_run(recorder, freezer):
    hass = recorder
    freezer.move_to(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    with (
        patch(
            "custom_components.discrete_statistics.Compiler.async_compile_incremental",
            return_value=0,
        ) as compile_mock,
        patch(
            "custom_components.discrete_statistics.get_instance",
            return_value=Mock(backlog=10_000),
        ),
    ):
        when = datetime(2026, 1, 1, 11, 3, 0, tzinfo=timezone.utc)
        freezer.move_to(when)
        async_fire_time_changed(hass, when)
        await hass.async_block_till_done()

    assert not compile_mock.called


async def test_runs_do_not_overlap(recorder, freezer):
    """Two concurrent runs must serialise; overlapping compiles corrupt sums."""
    hass = recorder
    freezer.move_to(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    concurrent = 0
    peak = 0

    async def fake_compile(self, cfg):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0)
        concurrent -= 1
        return 0

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        fake_compile,
    ):
        compile_all = hass.data[DOMAIN]["compile_all"]
        await asyncio.gather(compile_all(), compile_all())

    assert peak == 1


async def _seed_statistics(hass, statistic_id="discrete_statistics:binary_sensor_grid_status_on_duration"):
    """Write one discrete_statistics statistic, as a previous run would have."""
    async_add_external_statistics(
        hass,
        {
            "has_mean": True,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": True,
            "name": "Grid Status: on (duration)",
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_of_measurement": "h",
            "unit_class": "duration",
        },
        [
            {
                "start": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "sum": 5.0,
                "mean": 1.0,
                "min": 1.0,
                "max": 1.0,
            }
        ],
    )
    await get_instance(hass).async_block_till_done()


async def test_lost_registry_halts_compiling(recorder, freezer):
    """Statistics without the registry that describes them must stop everything.

    Every reader of the registry degrades silently at once: the cumulative
    base is not found, density collapses, and the recorder's upsert then
    rewrites the surviving rows downward. That is permanent.
    """
    hass = recorder
    await _seed_statistics(hass)  # statistics exist; .storage does not

    freezer.move_to(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    assert hass.data[DOMAIN]["halted"] is True

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ) as compile_mock:
        when = datetime(2026, 1, 1, 11, 3, 0, tzinfo=timezone.utc)
        freezer.move_to(when)
        async_fire_time_changed(hass, when)
        await hass.async_block_till_done()

    assert not compile_mock.called


async def test_lost_registry_raises_the_issue(recorder):
    hass = recorder
    await _seed_statistics(hass)
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, "registry_lost") is not None


async def test_lost_registry_refuses_the_recompute_service(recorder):
    """Invoked by hand, so it must say no rather than quietly do nothing."""
    hass = recorder
    await _seed_statistics(hass)
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "recompute", {"entity_id": ENTITY}, blocking=True
        )


async def test_a_fresh_install_is_not_mistaken_for_a_lost_registry(recorder, freezer):
    """Empty registry AND no statistics is simply a new install.

    This is the false positive that would break every first run.
    """
    hass = recorder
    freezer.move_to(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    assert hass.data[DOMAIN]["halted"] is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, "registry_lost") is None

    with patch(
        "custom_components.discrete_statistics.Compiler.async_compile_incremental",
        return_value=0,
    ) as compile_mock:
        when = datetime(2026, 1, 1, 11, 3, 0, tzinfo=timezone.utc)
        freezer.move_to(when)
        async_fire_time_changed(hass, when)
        await hass.async_block_till_done()

    assert compile_mock.called


async def test_other_sources_statistics_do_not_halt_us(recorder, freezer):
    """Only this integration's own statistics count as evidence."""
    hass = recorder
    async_add_external_statistics(
        hass,
        {
            "has_mean": False,
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": "Someone else",
            "source": "other_domain",
            "statistic_id": "other_domain:something",
            "unit_of_measurement": "kWh",
            "unit_class": "energy",
        },
        [{"start": datetime(2026, 1, 1, tzinfo=timezone.utc), "sum": 1.0}],
    )
    await get_instance(hass).async_block_till_done()

    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    assert hass.data[DOMAIN]["halted"] is False


async def test_a_populated_registry_is_never_halted(recorder):
    """Statistics plus the registry that describes them is the normal case."""
    hass = recorder
    await _seed_statistics(hass)

    registry = Registry(hass)
    await registry.async_load()
    await registry.async_register(
        ENTITY,
        {"discrete_statistics:binary_sensor_grid_status_on_duration": ("on", "duration")},
    )

    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()

    assert hass.data[DOMAIN]["halted"] is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, "registry_lost") is None
