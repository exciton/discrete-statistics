"""The read-only recorder queries the sensors and the card share."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata_with_session,
)
from homeassistant.components.recorder.util import session_scope
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from sqlalchemy.dialects import mysql

from custom_components.discrete_statistics import rows
from custom_components.discrete_statistics.buckets import Row, before_edges
from custom_components.discrete_statistics.const import METRIC_DURATION
from custom_components.discrete_statistics.payload import metadata_for
from custom_components.discrete_statistics.rows import bases, metadata_ids, standing

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


async def series_end(hass, ids):
    return await get_instance(hass).async_add_executor_job(rows.series_end, hass, ids)


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


async def test_series_end_is_the_newest_row_across_statistics(recorder):
    # OFF is the quiet state: its newest row is hours behind ON's.
    await seed(recorder, ON, T0, [0.5, 1.0, 1.5])
    await seed(recorder, OFF, T0, [0.5])
    assert (
        await series_end(recorder, {ON, OFF}) == (T0 + timedelta(hours=2)).timestamp()
    )
    assert (
        await series_end(recorder, {"discrete_statistics:nothing_on_duration"}) is None
    )


async def test_series_end_ignores_a_row_with_no_sum(recorder):
    await seed(recorder, ON, T0, [0.5, 1.0])
    async_add_external_statistics(
        recorder,
        metadata_for(METRIC_DURATION, ON, "Grid Status: On (h)"),
        [{"start": T0 + timedelta(hours=5), "mean": 1.0}],
    )
    await async_wait_recording_done(recorder)
    assert await series_end(recorder, {ON}) == (T0 + timedelta(hours=1)).timestamp()


async def test_bases_are_the_two_newest_rows_before_the_edge(recorder):
    hass = recorder
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # A: rows at hours 0, 1, 5 (a hole at 2-4). B: one row at hour 0. C: none.
    await seed(
        hass,
        "discrete_statistics:a_on_duration",
        start,
        [1.0, 2.0, None, None, None, 3.0],
    )
    await seed(hass, "discrete_statistics:b_on_duration", start, [7.0])

    edge = (start + timedelta(hours=6)).timestamp()
    found = await get_instance(hass).async_add_executor_job(
        bases,
        hass,
        {
            "discrete_statistics:a_on_duration",
            "discrete_statistics:b_on_duration",
            "discrete_statistics:c_on_duration",
        },
        edge,
    )

    a = found["discrete_statistics:a_on_duration"]
    assert [(r.start, r.sum) for r in a] == [
        ((start + timedelta(hours=5)).timestamp(), 3.0),
        ((start + timedelta(hours=1)).timestamp(), 2.0),
    ]
    assert [r.sum for r in found["discrete_statistics:b_on_duration"]] == [7.0]
    assert "discrete_statistics:c_on_duration" not in found


async def test_bases_stop_strictly_before_the_edge(recorder):
    hass = recorder
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await seed(hass, "discrete_statistics:a_on_duration", start, [1.0, 2.0, 3.0])

    # A row starting exactly on the edge is not before it.
    edge = (start + timedelta(hours=2)).timestamp()
    found = await get_instance(hass).async_add_executor_job(
        bases, hass, {"discrete_statistics:a_on_duration"}, edge
    )
    assert [r.sum for r in found["discrete_statistics:a_on_duration"]] == [2.0, 1.0]


async def test_standing_lists_the_hours_holding_a_row_inside_the_window(recorder):
    hass = recorder
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await seed(hass, "discrete_statistics:a_on_duration", start, [1.0, None, 2.0, 3.0])
    await seed(
        hass, "discrete_statistics:b_on_duration", start, [None, None, None, None, 4.0]
    )

    hours = await get_instance(hass).async_add_executor_job(
        standing,
        hass,
        {"discrete_statistics:a_on_duration", "discrete_statistics:b_on_duration"},
        (start + timedelta(hours=1)).timestamp(),
        (start + timedelta(hours=4)).timestamp(),
    )
    assert hours == {
        "discrete_statistics:a_on_duration": {
            (start + timedelta(hours=2)).timestamp(),
            (start + timedelta(hours=3)).timestamp(),
        }
    }


async def before(hass, pairs):
    """`rows_before` over (statistic_id, datetime) pairs, resolved per edge.

    The read answers with the distinct rows the pairs picked, and an edge
    is resolved over them by bisect - what `websocket` does through
    `buckets.before_edges`.
    """

    def read():
        with session_scope(hass=hass, read_only=True) as session:
            metadata = get_metadata_with_session(
                get_instance(hass), session, statistic_ids={sid for sid, _ in pairs}
            )
            ids = {sid: metadata_id for sid, (metadata_id, _) in metadata.items()}
            found = rows.rows_before(
                session, [(ids[sid], edge.timestamp()) for sid, edge in pairs]
            )
            return {
                (sid, edge): before_edges(found.get(ids[sid], ()), [edge.timestamp()])[
                    edge.timestamp()
                ]
                for sid, edge in pairs
            }

    return await get_instance(hass).async_add_executor_job(read)


async def test_rows_before_answers_each_pair_with_the_newest_row_before_its_edge(
    recorder,
):
    await seed(recorder, ON, T0, [0.5, 1.0, 1.5])
    await seed(recorder, OFF, T0, [0.25, None, 0.75])

    found = await before(
        recorder,
        [
            (ON, T0 + timedelta(hours=2)),
            (ON, T0 + timedelta(hours=3)),
            (OFF, T0 + timedelta(hours=2)),
        ],
    )

    # A row starting exactly at the edge is not before it.
    assert found[(ON, T0 + timedelta(hours=2))] == (
        (T0 + timedelta(hours=1)).timestamp(),
        1.0,
    )
    assert found[(ON, T0 + timedelta(hours=3))] == (
        (T0 + timedelta(hours=2)).timestamp(),
        1.5,
    )
    # OFF's hole at hour 1 carries its hour-0 row forward.
    assert found[(OFF, T0 + timedelta(hours=2))] == (T0.timestamp(), 0.25)


async def test_rows_before_leaves_a_pair_with_nothing_before_it_absent(recorder):
    await seed(recorder, ON, T0, [0.5])
    assert await before(recorder, [(ON, T0)]) == {(ON, T0): None}


async def test_rows_before_is_one_statement_on_sqlite_whatever_the_pair_count(
    recorder, statements
):
    await seed(recorder, ON, T0, [0.5])
    # Past the compound-select cap the arms would batch at; json_each does not.
    edges = [T0 + timedelta(hours=i + 1) for i in range(rows.SEEK_BATCH + 1)]

    statements.clear()
    found = await before(recorder, [(ON, edge) for edge in edges])

    assert len(statements) == 1
    assert found[(ON, edges[-1])] == (T0.timestamp(), 0.5)


async def both_paths(hass, pairs):
    """The rows the engine's own rendering answers with, and the arms'.

    SQLite runs the `json_each` path, so the arms every other engine gets
    are exercised here by building them explicitly.
    """

    def read():
        with session_scope(hass=hass, read_only=True) as session:
            metadata = get_metadata_with_session(
                get_instance(hass), session, statistic_ids={sid for sid, _ in pairs}
            )
            ids = {sid: metadata_id for sid, (metadata_id, _) in metadata.items()}
            keyed = list(
                dict.fromkeys((ids[sid], edge.timestamp()) for sid, edge in pairs)
            )
            expanded = rows.rows_before(session, keyed)
            armed: dict[int, set[Row]] = {}
            for statement in rows.seek_statements(None, keyed):
                for metadata_id, start_ts, sum_ in session.execute(statement):
                    armed.setdefault(metadata_id, set()).add(Row(start_ts, sum_))
            named = {metadata_id: sid for sid, metadata_id in ids.items()}
            return (
                {named[key]: series for key, series in expanded.items()},
                {named[key]: sorted(series) for key, series in armed.items()},
            )

    return await get_instance(hass).async_add_executor_job(read)


async def test_the_arms_answer_every_pair_as_the_expanded_form_does(
    recorder, statements, fetched
):
    await seed(recorder, ON, T0, [0.5, 1.0, 1.5])
    # A row with no sum, newest before the later edges: an arm that drops
    # `sum IS NOT NULL` would answer with it.
    async_add_external_statistics(
        recorder,
        metadata_for(METRIC_DURATION, ON, "Grid Status: On (h)"),
        [{"start": T0 + timedelta(hours=3)}],
    )
    await async_wait_recording_done(recorder)
    # Past the batch, so the arms really run in two statements.
    edges = [T0 + timedelta(hours=i) for i in range(rows.SEEK_BATCH + 1)]

    statements.clear()
    fetched.clear()
    expanded, armed = await both_paths(recorder, [(ON, edge) for edge in edges])

    assert armed == expanded
    # Three distinct rows through the expanded form, and four through the
    # arms - each batch is distinct in itself, and the hour-2 row answers
    # an edge in both - rather than five hundred and one either way.
    assert fetched.rows == 7
    # One statement for the expanded form, two batches of arms.
    assert len(statements) == 3
    # Three rows for five hundred edges: each distinct answer once, and the
    # sumless hour-3 row is not one of them.
    assert armed[ON] == [
        (T0.timestamp(), 0.5),
        ((T0 + timedelta(hours=1)).timestamp(), 1.0),
        ((T0 + timedelta(hours=2)).timestamp(), 1.5),
    ]
    resolved = before_edges(armed[ON], [edge.timestamp() for edge in edges])
    assert resolved[edges[2].timestamp()] == (
        (T0 + timedelta(hours=1)).timestamp(),
        1.0,
    )
    assert resolved[edges[500].timestamp()] == (
        (T0 + timedelta(hours=2)).timestamp(),
        1.5,
    )
    # Nothing stands before the first edge.
    assert resolved[edges[0].timestamp()] is None


async def test_rows_before_returns_one_row_however_many_edges_it_answers(
    recorder, statements, fetched
):
    # A rare state: one row, and a year of monthly edges after it. The
    # statement returns the row once, not once per edge.
    await seed(recorder, ON, T0, [0.5])
    edges = [T0 + timedelta(days=30 * i) for i in range(1, 14)]

    statements.clear()
    fetched.clear()
    found = await before(recorder, [(ON, edge) for edge in edges])

    assert set(found.values()) == {(T0.timestamp(), 0.5)}
    assert len(statements) == 1
    # One row for thirteen edges; returning the edge with it would return
    # the same row thirteen times.
    assert fetched.rows == 1


def arms(sql: str) -> int:
    return sql.count("UNION ALL") + 1


PAIRS = [(7, 1789038000.0 + hour * 3600) for hour in range(rows.SEEK_BATCH + 1)]


def test_the_mysql_rendering_is_constant_bound_arms_batched_at_five_hundred():
    built = [
        str(stmt.compile(dialect=mysql.dialect()))
        for stmt in rows.seek_statements("mysql", PAIRS)
    ]

    assert [arms(sql) for sql in built] == [rows.SEEK_BATCH, 1]
    assert all("json" not in sql.lower() for sql in built)


def test_the_sqlite_rendering_seeks_once_per_json_each_pair():
    built = rows.seek_statements("sqlite", PAIRS)

    assert len(built) == 1
    sql = built[0].text
    assert "json_each" in sql
    assert "ORDER BY" in sql and "start_ts DESC" in sql and "LIMIT 1" in sql
    assert "sum IS NOT NULL" in sql
    assert "UNION ALL" not in sql


def test_the_postgresql_rendering_seeks_once_per_jsonb_element():
    built = rows.seek_statements("postgresql", PAIRS)

    assert len(built) == 1
    sql = built[0].text
    assert "jsonb_array_elements" in sql
    assert "start_ts DESC" in sql and "LIMIT 1" in sql
    # Filtering the picked id would have Postgres evaluate the subplan twice.
    assert "id IS NOT NULL" not in sql
    assert "UNION ALL" not in sql


def test_the_series_start_rendering_is_one_ascending_seek_per_statistic():
    sql = str(
        rows._earliest([7, 9, 11]).compile(
            dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    # One arm per statistic, each an index seek, and no aggregate: a
    # `min(start_ts)` over the set scans on Postgres.
    assert arms(sql) == 3
    assert sql.count("LIMIT 1") == 3
    assert "min(" not in sql.lower()
    assert "DESC" not in sql
    assert sql.count("sum IS NOT NULL") == 3


def test_the_series_end_rendering_is_one_descending_seek_per_statistic():
    sql = str(
        rows._latest([7, 9, 11]).compile(
            dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    # One arm per statistic, each an index seek from the newest end, and
    # no aggregate: the watermark is the max taken in Python.
    assert arms(sql) == 3
    assert sql.count("LIMIT 1") == 3
    assert sql.count("start_ts DESC") == 3
    assert "max(" not in sql.lower()
    assert sql.count("sum IS NOT NULL") == 3


def test_an_unknown_engine_falls_back_to_the_arms():
    built = rows.seek_statements(None, PAIRS)

    assert [arms(str(stmt.compile(dialect=mysql.dialect()))) for stmt in built] == [
        rows.SEEK_BATCH,
        1,
    ]


async def test_rows_from_opens_each_statistic_on_the_row_before_the_range(
    recorder, statements
):
    # ON runs from before the range; OFF begins inside it; a third
    # statistic has no row at all.
    await seed(recorder, ON, T0, [0.5, 1.0, 1.5, 2.0])
    await seed(recorder, OFF, T0 + timedelta(hours=2), [0.25, 0.5])
    await seed(recorder, "discrete_statistics:nothing_on_duration", T0, [])

    def read():
        with session_scope(hass=recorder, read_only=True) as session:
            metadata = get_metadata_with_session(
                get_instance(recorder),
                session,
                statistic_ids={ON, OFF, "discrete_statistics:nothing_on_duration"},
            )
            ids = {sid: metadata_id for sid, (metadata_id, _) in metadata.items()}
            found = rows.rows_from(
                session,
                set(ids.values()),
                (T0 + timedelta(hours=2)).timestamp(),
                (T0 + timedelta(hours=4)).timestamp(),
            )
            return {sid: found.get(mid) for sid, mid in ids.items()}

    statements.clear()
    found = await get_instance(recorder).async_add_executor_job(read)

    assert len(statements) == 1
    hours = [(T0 + timedelta(hours=i)).timestamp() for i in range(4)]
    assert found[ON] == [(hours[1], 1.0), (hours[2], 1.5), (hours[3], 2.0)]
    assert found[OFF] == [(hours[2], 0.25), (hours[3], 0.5)]
    assert found["discrete_statistics:nothing_on_duration"] is None


async def read_metadata(hass, statistic_ids, slugs):
    def read():
        with session_scope(hass=hass, read_only=True) as session:
            return metadata_ids(session, statistic_ids, slugs)

    return await get_instance(hass).async_add_executor_job(read)


async def test_metadata_ids_reads_the_entity_asked_for_and_not_the_source(recorder):
    hass = recorder
    longer = "discrete_statistics:binary_sensor_grid_status_backup_on_duration"
    await seed(hass, ON, T0, [0.5])
    await seed(hass, OFF, T0, [0.5])
    await seed(hass, longer, T0, [0.5])
    await seed(hass, "discrete_statistics:light_kitchen_on_duration", T0, [0.5])

    found = await read_metadata(hass, {ON}, {"binary_sensor_grid_status"})

    # The entity's siblings, and nothing else we hold: not the other
    # entity, and not the longer slug the LIKE prefix also matches.
    assert set(found) == {ON, OFF}
    assert all(isinstance(metadata_id, int) for metadata_id in found.values())


async def test_metadata_ids_answers_an_id_of_ours_with_no_entity_slug_asked(recorder):
    # An ID that does not parse has no siblings to find; it is still ours,
    # and is read by name.
    hass = recorder
    odd = "discrete_statistics:renamed_by_hand"
    await seed(hass, odd, T0, [0.5])
    await seed(hass, ON, T0, [0.5])

    assert set(await read_metadata(hass, {odd}, set())) == {odd}
    assert await read_metadata(hass, set(), set()) == {}


async def test_metadata_ids_costs_no_statistics_row(recorder, statements):
    await seed(recorder, ON, T0, [0.5])

    statements.clear()
    await read_metadata(recorder, {ON}, {"binary_sensor_grid_status"})

    assert statements == []
