"""The card's bucket command, end to end through the recorder."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from sqlalchemy import event as sqlalchemy_event

from custom_components.discrete_statistics.const import DOMAIN, METRIC_DURATION
from custom_components.discrete_statistics.payload import metadata_for

ENTITY = "binary_sensor.grid_status"
ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"
OTHER = "discrete_statistics:binary_sensor_porch_light_on_duration"
OTHER_OFF = "discrete_statistics:binary_sensor_porch_light_off_duration"
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


@pytest.fixture
def statements(hass, recorder_mock):
    """The SELECTs the recorder's engine runs against the statistics table."""
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


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


async def test_days_in_the_instance_timezone(hass, client):
    # Two full local days at a quarter hour per hour; the range asked for
    # starts mid-morning and the buckets still begin at local midnight.
    seed(hass, ON, local(2026, 3, 2), [0.25 * (i + 1) for i in range(48)])
    await async_wait_recording_done(hass)

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


async def test_every_statistic_is_answered_and_a_hole_is_in_neither_bucket(
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
    await async_wait_recording_done(hass)

    response = await ask(client, [ON, OFF], local(2026, 3, 2), local(2026, 3, 4))

    assert response["result"][ON] == [
        {
            "start": ms(local(2026, 3, 2)),
            "end": ms(local(2026, 3, 3)),
            "change": 24.0,
        },
        {
            "start": ms(local(2026, 3, 3)),
            "end": ms(local(2026, 3, 4)),
            "change": 24.0,
        },
    ]
    assert response["result"][OFF] == [
        {
            "start": ms(local(2026, 3, 2)),
            "end": ms(local(2026, 3, 3)),
            "change": 20.0,
        },
        {
            "start": ms(local(2026, 3, 3)),
            "end": ms(local(2026, 3, 4)),
            "change": 18.0,
        },
    ]


async def test_the_change_is_against_the_sum_before_the_range(hass, client):
    # A series that has been running for a day before the range; the first
    # bucket's change is the difference from its last row, not from zero.
    seed(hass, ON, local(2026, 3, 1), [float(i + 1) for i in range(48)])
    await async_wait_recording_done(hass)

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
    await async_wait_recording_done(hass)

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


async def test_hours_are_the_rows_themselves(hass, client):
    # Hourly buckets read every row in the range; the range's ends land
    # mid-hour and the buckets still cover whole hours, a hole among them
    # left out.
    seed(hass, ON, local(2026, 3, 2), [1.0, 2.0, None, 3.0, 4.0])
    await async_wait_recording_done(hass)

    response = await ask(
        client, [ON], local(2026, 3, 2, 0, 30), local(2026, 3, 2, 3, 30), "hour"
    )

    assert response["result"] == {
        ON: [
            {
                "start": ms(local(2026, 3, 2, 0)),
                "end": ms(local(2026, 3, 2, 1)),
                "change": 1.0,
            },
            {
                "start": ms(local(2026, 3, 2, 1)),
                "end": ms(local(2026, 3, 2, 2)),
                "change": 1.0,
            },
            {
                "start": ms(local(2026, 3, 2, 3)),
                "end": ms(local(2026, 3, 2, 4)),
                "change": 1.0,
            },
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


async def test_bad_ranges_are_refused(hass, client):
    response = await ask(client, [ON], local(2026, 3, 3), local(2026, 3, 3))
    assert response["error"]["code"] == "invalid_range"

    # Two years of hours is more than any chart shows, and the edges alone
    # would be walked on the recorder's thread.
    response = await ask(client, [ON], local(2024, 3, 3), local(2026, 3, 3), "hour")
    assert response["error"]["code"] == "range_too_long"

    response = await ask(client, [ON], local(2024, 3, 3), local(2026, 3, 3), "day")
    assert response["success"]


async def stock(client, ids, start, end, period):
    """The same question put to recorder/statistics_during_period."""
    await client.send_json_auto_id(
        {
            "type": "recorder/statistics_during_period",
            "statistic_ids": ids,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "period": period,
            "types": ["change"],
        }
    )
    response = await client.receive_json()
    assert response["success"]
    return response["result"]


# Twenty weeks from a Monday that is also the first of the month, so one
# range is aligned for every period, holding: ON running since before the
# range with a hole straddling a day edge, then a hole spanning whole days
# and weeks, and OFF beginning part-way through a bucket of every period.
# Both run a day past the range's end. Sydney changes clocks inside it.
TWENTY_WEEKS = 20 * 7 * 24
ON_SUMS: list[float | None] = [
    None if 30 <= i < 40 or 3 * 24 * 7 + 5 <= i < 5 * 24 * 7 + 5 else 0.5 * i
    for i in range(48 + TWENTY_WEEKS)
]
OFF_SUMS: list[float | None] = [
    None if i < 24 + 24 * 7 + 3 * 24 + 11 else 0.25 * i
    for i in range(48 + TWENTY_WEEKS)
]


ALIGNED = ((2026, 6, 1), (2026, 10, 19))
# Hour-aligned but not period-aligned, as the card asks.
MID_DAY = ((2026, 6, 1, 9), (2026, 10, 18, 15))


@pytest.mark.parametrize("span", [ALIGNED, MID_DAY])
# Kolkata is five and a half hours off UTC: every edge is at half past,
# so no row starts the hour before one.
@pytest.mark.parametrize("zone", ["Australia/Sydney", "Asia/Kolkata"])
async def test_buckets_match_the_recorder(hass, client, zone, span):
    await hass.config.async_set_time_zone(zone)
    tz = ZoneInfo(zone)
    start, end = (datetime(*when, tzinfo=tz) for when in span)
    # Rows start on UTC hours whatever the zone.
    first = datetime(2026, 5, 31, tzinfo=tz).astimezone(UTC).replace(minute=0)
    seed(hass, ON, first, ON_SUMS)
    seed(hass, OFF, first, OFF_SUMS)
    await async_wait_recording_done(hass)

    # Compared inside the range: both snap the first period outward, but
    # the recorder only for a day or longer, and it answers an end on an
    # edge with the period after it too, where ours stays inside.
    def inside(result):
        return {
            statistic_id: [b for b in buckets if ms(start) <= b["start"] < ms(end)]
            for statistic_id, buckets in result.items()
        }

    for period in ("hour", "day", "week", "month", "year"):
        ours = await ask(client, [ON, OFF], start, end, period)
        theirs = await stock(client, [ON, OFF], start, end, period)

        assert ours["success"], period
        mine, stock_ = inside(ours["result"]), inside(theirs)
        for statistic_id in (ON, OFF):
            by_start = {b["start"]: b for b in mine[statistic_id]}
            assert [by_start[b["start"]] for b in stock_[statistic_id]] == stock_[
                statistic_id
            ], (period, statistic_id)
            extra = [
                b
                for b in mine[statistic_id]
                if b["start"] not in {t["start"] for t in stock_[statistic_id]}
            ]
            assert all(b["change"] == 0.0 for b in extra), (period, statistic_id)
        assert len(ours["result"][ON]) > 2 or period == "year"
        assert len(ours["result"][OFF]) > 1 or period == "year"


async def test_a_rare_state_is_zero_in_a_compiled_month_and_absent_in_a_hole(
    hass, client
):
    """Gap or zero is judged on the entity's duration rows as a whole."""
    await hass.config.async_set_time_zone("UTC")
    jan, feb, mar, apr = (
        utc(2026, 1, 1),
        utc(2026, 2, 1),
        utc(2026, 3, 1),
        utc(2026, 4, 1),
    )
    # ON is the state held: a row every hour in January and March, none in
    # February. OFF happened once, in January.
    seed(hass, ON, jan, [float(i) for i in range(31 * 24)])
    seed(hass, ON, mar, [1000.0 + i for i in range(31 * 24)])
    seed(hass, OFF, jan + timedelta(days=10, hours=5), [1.0])
    await async_wait_recording_done(hass)

    response = await ask(client, [OFF], jan, apr, "month")

    assert response["result"][OFF] == [
        {"start": ms(jan), "end": ms(feb), "change": 1.0},
        {"start": ms(mar), "end": ms(apr), "change": 0.0},
    ]


async def test_a_rare_state_costs_one_seek(hass, client, statements):
    await hass.config.async_set_time_zone("UTC")
    jan, apr = utc(2026, 1, 1), utc(2026, 4, 1)
    # ON held throughout, from before the range, so every edge's row is
    # in the IN query. OFF happened three times.
    seed(hass, ON, jan - timedelta(days=1), [float(i) for i in range(91 * 24)])
    for day in (10, 40, 70):
        seed(hass, OFF, jan + timedelta(days=day, hours=5), [float(day)])
    await async_wait_recording_done(hass)

    statements.clear()
    response = await ask(client, [OFF], jan, apr, "month")

    assert [b["change"] for b in response["result"][OFF]] == [10.0, 30.0, 30.0]
    # The IN query, then one seek for OFF that returns its whole series.
    assert len(statements) == 2


async def test_a_busy_statistic_under_monthly_edges_seeks_once_per_hole(
    hass, client, statements
):
    await hass.config.async_set_time_zone("UTC")
    jan, apr = utc(2026, 1, 1), utc(2026, 4, 1)
    days = (apr - jan).days
    # OFF held: every hour, from before the range. ON: hours 0-11 of every
    # day, so never in the hour before an edge.
    seed(hass, OFF, jan - timedelta(days=1), [float(i) for i in range((days + 1) * 24)])
    seed(hass, ON, jan, [float(i) if i % 24 < 12 else None for i in range(days * 24)])
    await async_wait_recording_done(hass)

    statements.clear()
    response = await ask(client, [ON], jan, apr, "month")

    assert [b["change"] for b in response["result"][ON]] == [
        pytest.approx(31 * 24 - 13),
        pytest.approx(28 * 24),
        pytest.approx(31 * 24),
    ]
    # The IN query, then a seek at each of the four edges - the last one
    # finds nothing before January and settles the rest. Never a range.
    assert len(statements) == 5
    assert not any("start_ts >=" in s for s in statements[1:])


async def test_a_daily_statistic_under_daily_edges_reads_one_range(
    hass, client, statements
):
    await hass.config.async_set_time_zone("UTC")
    start = utc(2026, 1, 1)
    end = start + timedelta(days=60)
    # OTHER_OFF held: every hour, from before the range. OTHER: 18:00 to
    # 21:00 every day.
    seed(hass, OTHER_OFF, start - timedelta(days=1), [float(i) for i in range(61 * 24)])
    seed(
        hass,
        OTHER,
        start,
        [float(i) if 18 <= i % 24 <= 21 else None for i in range(60 * 24)],
    )
    await async_wait_recording_done(hass)

    statements.clear()
    response = await ask(client, [OTHER], start, end, "day")

    buckets = response["result"][OTHER]
    assert len(buckets) == 60
    # Each day's change is the step from the previous day's last row (21:00)
    # to this day's (21:00): 24.
    assert [b["change"] for b in buckets[1:]] == [pytest.approx(24.0)] * 59
    # The IN query, one seek, one range, and one seek for the first edge,
    # which the range cannot settle - nothing is known before it.
    assert len(statements) == 4
    assert sum("start_ts >=" in s for s in statements[1:]) == 1


async def test_two_entities_are_each_judged_on_their_own_rows(hass, client):
    await hass.config.async_set_time_zone("UTC")
    jan, mar = utc(2026, 1, 1), utc(2026, 3, 1)
    seed(hass, ON, jan, [float(i) for i in range(59 * 24)])
    seed(hass, OTHER, jan, [float(i) for i in range(31 * 24)])
    # The states asked for exist but never occurred: metadata, no rows.
    seed(hass, OFF, jan, [])
    seed(hass, OTHER_OFF, jan, [])
    await async_wait_recording_done(hass)

    response = await ask(client, [OFF, OTHER_OFF], jan, mar, "month")

    # OFF's entity was compiled both months, OTHER_OFF's only in January.
    assert [b["change"] for b in response["result"][OFF]] == [0.0, 0.0]
    assert [b["start"] for b in response["result"][OTHER_OFF]] == [ms(jan)]
