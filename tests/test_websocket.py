"""The card's bucket command, end to end through the recorder."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.setup import async_setup_component

from custom_components.discrete_statistics.const import DOMAIN, METRIC_DURATION
from custom_components.discrete_statistics.payload import metadata_for

ENTITY = "binary_sensor.grid_status"
ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"
CONFIG = {DOMAIN: [{"entity_id": ENTITY, "name": "Grid Status"}]}
TZ = ZoneInfo("Australia/Sydney")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Override the root conftest fixture.

    The root fixture pulls in `hass`, which the recorder fixtures refuse to
    run behind: `recorder_db_url` asserts that hass has not been created
    yet. Requesting it first restores the required order.
    """
    yield


@pytest.fixture
async def client(hass, recorder_mock, hass_ws_client):
    """A websocket client on a hass with the integration set up."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    await async_setup_component(hass, "recorder", {"recorder": {}})
    assert await async_setup_component(hass, DOMAIN, CONFIG)
    await hass.async_block_till_done()
    return await hass_ws_client()


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


def ms(when: datetime) -> float:
    return when.timestamp() * 1000


def seed(hass, statistic_id: str, start: datetime, sums: list[float | None]) -> None:
    """Write one row per hour from start; None leaves that hour a hole."""
    async_add_external_statistics(
        hass,
        metadata_for(
            METRIC_DURATION, statistic_id, f"Grid Status: On ({METRIC_DURATION})"
        ),
        [
            {"start": start + timedelta(hours=i), "sum": value}
            for i, value in enumerate(sums)
            if value is not None
        ],
    )


async def ask(client, ids, start, end, period="day"):
    await client.send_json_auto_id(
        {
            "type": "discrete_statistics/buckets",
            "statistic_ids": ids,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "period": period,
        }
    )
    return await client.receive_json()


async def test_days_in_the_instance_timezone(hass, client):
    # Two full local days at a quarter hour per hour; the range asked for
    # starts mid-morning and the buckets still begin at local midnight.
    seed(hass, ON, local(2026, 3, 2), [0.25 * (i + 1) for i in range(48)])
    await get_instance(hass).async_block_till_done()

    response = await ask(client, [ON], local(2026, 3, 2, 9), local(2026, 3, 3, 15))

    assert response["success"]
    assert response["result"] == {
        ON: [
            {
                "start": ms(local(2026, 3, 2)),
                "end": ms(local(2026, 3, 3)),
                "change": 6.0,
            },
            {
                "start": ms(local(2026, 3, 3)),
                "end": ms(local(2026, 3, 4)),
                "change": 6.0,
            },
        ]
    }


async def test_every_statistic_is_answered_and_a_hole_shortens_its_buckets(
    hass, client
):
    seed(hass, ON, local(2026, 3, 2), [float(i + 1) for i in range(48)])
    # OFF is missing hours 20-29, straddling the day edge.
    seed(
        hass,
        OFF,
        local(2026, 3, 2),
        [float(i + 1) for i in range(20)]
        + [None] * 10
        + [20.0 + i for i in range(1, 19)],
    )
    await get_instance(hass).async_block_till_done()

    response = await ask(client, [ON, OFF], local(2026, 3, 2), local(2026, 3, 4))

    assert response["result"][ON] == [
        {"start": ms(local(2026, 3, 2)), "end": ms(local(2026, 3, 3)), "change": 24.0},
        {"start": ms(local(2026, 3, 3)), "end": ms(local(2026, 3, 4)), "change": 24.0},
    ]
    assert response["result"][OFF] == [
        {
            "start": ms(local(2026, 3, 2)),
            "end": ms(local(2026, 3, 2, 20)),
            "change": 20.0,
        },
        {
            "start": ms(local(2026, 3, 3, 6)),
            "end": ms(local(2026, 3, 4)),
            "change": 18.0,
        },
    ]


async def test_the_change_is_against_the_sum_before_the_range(hass, client):
    # A series that has been running for a day before the range; the first
    # bucket's change is the difference from its last row, not from zero.
    seed(hass, ON, local(2026, 3, 1), [float(i + 1) for i in range(48)])
    await get_instance(hass).async_block_till_done()

    response = await ask(client, [ON], local(2026, 3, 2), local(2026, 3, 3))

    assert response["result"] == {
        ON: [
            {
                "start": ms(local(2026, 3, 2)),
                "end": ms(local(2026, 3, 3)),
                "change": 24.0,
            }
        ]
    }


async def test_a_month_of_daily_rows_is_one_bucket(hass, client):
    seed(hass, ON, local(2026, 2, 1), [0.5 * (i + 1) for i in range(28 * 24)])
    await get_instance(hass).async_block_till_done()

    response = await ask(client, [ON], local(2026, 2, 10), local(2026, 2, 20), "month")

    assert response["result"] == {
        ON: [
            {
                "start": ms(local(2026, 2, 1)),
                "end": ms(local(2026, 3, 1)),
                "change": 336.0,
            }
        ]
    }


async def test_an_unknown_statistic_is_absent(hass, client):
    response = await ask(client, [ON], local(2026, 3, 2), local(2026, 3, 3))

    assert response["success"]
    assert response["result"] == {}


async def test_bad_times_are_refused(hass, client):
    await client.send_json_auto_id(
        {
            "type": "discrete_statistics/buckets",
            "statistic_ids": [ON],
            "start_time": "yesterday",
            "end_time": local(2026, 3, 3).isoformat(),
            "period": "day",
        }
    )
    response = await client.receive_json()
    assert not response["success"]
    assert response["error"]["code"] == "invalid_start_time"

    await client.send_json_auto_id(
        {
            "type": "discrete_statistics/buckets",
            "statistic_ids": [ON],
            "start_time": local(2026, 3, 2).isoformat(),
            "end_time": "tomorrow",
            "period": "day",
        }
    )
    response = await client.receive_json()
    assert response["error"]["code"] == "invalid_end_time"
