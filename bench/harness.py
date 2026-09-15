"""The integration's recorder reads, timed and counted.

Not a test of behaviour - it measures. `bench/test_bench.py` is the pytest
driver; everything it does lives here so `bench/test_selftest.py` can run
the same code against a database it builds itself.

One `Run` describes the run (mode, engine, repeat count, where the data and
the results live), one `Cases` describes what to measure.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics as stats
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from homeassistant.components.history_stats.data import HistoryStats
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.helpers.template import Template
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import text as sql_text

from custom_components.discrete_statistics import compiler as compiler_module
from custom_components.discrete_statistics import periods, reading, websocket
from custom_components.discrete_statistics import rows as ds_rows
from custom_components.discrete_statistics.compiler import Compiler
from custom_components.discrete_statistics.config import entity_config_from_entry
from custom_components.discrete_statistics.const import HOUR
from custom_components.discrete_statistics.coordinator import frame_of
from custom_components.discrete_statistics.reading import Partial, Spec

from . import cases as cases_module
from . import profiling

DAY = 24 * HOUR


@dataclass(frozen=True)
class Run:
    """What `script/bench` tells the harness, through `run.json`.

    A file rather than environment variables: pytest runs inside the
    container and inherits no host environment, while the file is inside
    the bind mount and is written immediately before the run.
    """

    mode: str = "measure"
    repeat: int = 5
    engine: str = "sqlite"
    variant: str = "default"
    branch: str = "working"
    revision: str = ""
    cases: str = "bench/cases.yaml"
    data_dir: str = "bench/data"
    results_dir: str = "bench/results"
    # A SQLite database whose newest compiled hour a `build` stops at, so
    # every database built covers the same span. Absent: build to now.
    build_anchor: str | None = None
    # BENCH_PROFILE: wrap each entity's compile in cProfile and the phase
    # split, and write a report per entity under `results/profiles/`.
    profile: bool = False
    # BENCH_CHUNK_HOURS: the compiler's chunk size for this run, so how
    # much of the cost is per chunk can be measured. The module constant
    # is set, not the integration changed.
    chunk_hours: int | None = None


def load_run(path: str | Path) -> Run:
    document = json.loads(Path(path).read_text())
    fields = {f for f in Run.__dataclass_fields__}
    return Run(**{k: v for k, v in document.items() if k in fields})


# --------------------------------------------------------------------- the database

# Asked through the recorder's own engine, so one query serves SQLite,
# MariaDB and Postgres alike.
_OURS = (
    "FROM statistics s JOIN statistics_meta m ON m.id = s.metadata_id"
    " WHERE m.source = 'discrete_statistics'"
)


async def _scalar(hass, sql: str):
    # In the recorder's executor: for SQLite the engine's RecorderPool
    # refuses a connection from any other thread.
    def query():
        with get_instance(hass).engine.connect() as conn:
            return conn.execute(sql_text(sql)).scalar()

    return await get_instance(hass).async_add_executor_job(query)


async def anchor(hass) -> float | None:
    """The end of the newest hour our statistics hold in this database."""
    newest = await _scalar(hass, f"SELECT MAX(s.start_ts) {_OURS}")
    return None if newest is None else float(newest) + HOUR


async def our_rows(hass) -> int:
    return int(await _scalar(hass, f"SELECT COUNT(*) {_OURS}") or 0)


def sqlite_anchor(db: str | Path) -> float | None:
    """The same, read straight off a SQLite file - a `build` stops where
    another database does whichever engine it is filling."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        newest = con.execute(
            "SELECT MAX(start_ts) FROM statistics s JOIN statistics_meta m"
            " ON m.id = s.metadata_id WHERE m.source = 'discrete_statistics'"
        ).fetchone()[0]
    finally:
        con.close()
    return None if newest is None else newest + HOUR


def configs(run: Run) -> list:
    """One `EntityConfig` per config entry in the data directory."""
    return [
        entity_config_from_entry(e["data"], e["options"])
        for e in cases_module.entries(run.data_dir)
    ]


def wanted_configs(run: Run, cases=None) -> list:
    """The configs a writing mode runs over: the cases' `entities`, or all."""
    every = configs(run)
    names = set(cases.entities) if cases and cases.entities else None
    return [cfg for cfg in every if names is None or cfg.entity_id in names]


@contextmanager
def profiler_for(hass, run: Run):
    """The profiler for a writing mode, when the run asked for one.

    Also where `chunk_hours` is applied: both are the same opt-in
    investigation, both are set on the compiler module and taken off
    again, and neither touches the integration's own source.
    """
    original_chunk = compiler_module.CHUNK_HOURS
    if run.chunk_hours:
        compiler_module.CHUNK_HOURS = run.chunk_hours
        print(f"chunk_hours {run.chunk_hours}", flush=True)
    profiler = None
    if run.profile:
        profiler = profiling.Profiler(
            hass,
            _results_dir(run.results_dir) / "profiles",
            f"{run.branch}-{run.engine}-{run.variant}-chunk{compiler_module.CHUNK_HOURS}",
        )
    try:
        yield profiler
    finally:
        if profiler is not None:
            profiler.close()
        compiler_module.CHUNK_HOURS = original_chunk


@contextmanager
def entity_profile(profiler, label: str):
    if profiler is None:
        yield
        return
    with profiler.entity(label):
        yield


# --------------------------------------------------------------------- meter


_STATISTICS = re.compile(r"\bstatistics\b", re.IGNORECASE)


class Meter:
    """Statements, statistics-table statements and rows fetched, per window.

    Statements come from SQLAlchemy's own event, so anything the
    recorder's thread runs inside the window is counted too - the bench
    hass is idle and drained before each run, so that is noise.
    """

    def __init__(self, hass) -> None:
        engine = get_instance(hass).engine
        self.on = False
        self.reset()
        sqlalchemy_event.listen(engine, "before_cursor_execute", self._before)
        sqlalchemy_event.listen(engine, "after_cursor_execute", self._after)
        sqlalchemy_event.listen(engine, "connect", self._connect)
        sqlalchemy_event.listen(engine, "checkout", self._checkout)
        self.capture = False
        self.dialect = engine.dialect.name

    def reset(self) -> None:
        self.statements = 0
        self.statistics = 0
        self.rows = 0
        # Statements against the statistics tables, for EXPLAIN. Collected
        # only while `capture` is set, which is one (warm-up) pass per case.
        self.captured: list[tuple[str, object]] = []
        # The statements themselves, so a case can show what it read.
        self.texts: list[str] = []

    def _row(self, cursor, row):
        if self.on:
            self.rows += 1
        return row

    def _factory(self, dbapi_connection) -> None:
        # Only sqlite3 has one; psycopg2's connection has no __dict__ at
        # all, so this is a try rather than a hasattr.
        try:
            dbapi_connection.row_factory = self._row
        except AttributeError:
            pass

    def _connect(self, dbapi_connection, record):
        self._factory(dbapi_connection)

    def _checkout(self, dbapi_connection, record, proxy):
        self._factory(dbapi_connection)

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        # pymysql and psycopg2 have no row_factory but do set `rowcount`
        # for a SELECT, which sqlite3 leaves at -1: every engine counted
        # once, none twice.
        if self.on and cursor.rowcount and cursor.rowcount > 0:
            self.rows += cursor.rowcount

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        if not self.on:
            return
        self.statements += 1
        if len(self.texts) < 50:
            self.texts.append(" ".join(statement.split()))
        if _STATISTICS.search(statement):
            self.statistics += 1
            if self.capture:
                # executemany hands a sequence of parameter sets; the
                # first one is representative and is what we explain.
                params = parameters[0] if executemany and parameters else parameters
                self.captured.append((statement, params))

    @contextmanager
    def window(self):
        self.reset()
        self.on = True
        try:
            yield
        finally:
            self.on = False


# --------------------------------------------------------------------- explain


_EXPLAIN = {
    "sqlite": "EXPLAIN QUERY PLAN ",
    "mysql": "EXPLAIN FORMAT=JSON ",
    "postgresql": "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ",
}


def _literal(value) -> str:
    """A bound parameter as SQL text. For EXPLAIN only - see the header
    the plan files carry."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _inline(statement: str, parameters) -> str:
    """Substitute the bound parameters into the statement.

    The drivers differ in paramstyle - qmark for sqlite3, format for
    pymysql and psycopg2, pyformat when SQLAlchemy names them - so both
    the positional and the named forms are handled.
    """
    if isinstance(parameters, dict):
        out = statement
        for key in sorted(parameters, key=len, reverse=True):
            literal = _literal(parameters[key])
            out = out.replace(f"%({key})s", literal).replace(f":{key}", literal)
        return out
    values = list(parameters or ())
    parts: list[str] = []
    index = 0
    i = 0
    while i < len(statement):
        char = statement[i]
        if char == "?" and index < len(values):
            parts.append(_literal(values[index]))
            index += 1
        elif statement.startswith("%s", i) and index < len(values):
            parts.append(_literal(values[index]))
            index += 1
            i += 1
        else:
            parts.append(char)
        i += 1
    return "".join(parts)


def _slug(case: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", case.lower()).strip("-")


async def explain(hass, captured: list[tuple[str, object]], out: Path) -> None:
    """Run the engine's own explain over every statistics-table statement
    one case issued, and save the plans."""
    prefix = _EXPLAIN[get_instance(hass).engine.dialect.name]
    seen: set[str] = set()
    chunks = [
        (
            "-- Plans for the statistics-table statements of this case.\n"
            "-- The statements are run with bound parameters; the SQL below has\n"
            "-- the parameters substituted in, FOR THE EXPLAIN ONLY. The measured\n"
            f"-- statements themselves are unchanged.\n-- explain: {prefix.strip()}\n"
        )
    ]

    def one(sql: str) -> str:
        with get_instance(hass).engine.connect() as conn:
            # exec_driver_sql, not text(): a statistic ID contains a colon
            # and text() would read `:light_kitchen_...` as a bind name.
            plan_rows = conn.exec_driver_sql(prefix + sql).fetchall()
        # MariaDB and Postgres answer in JSON, which psycopg2 has already
        # parsed; SQLite answers in columns.
        lines = []
        for row in plan_rows:
            for value in row:
                if isinstance(value, (list, dict)):
                    lines.append(json.dumps(value, indent=2))
                    break
            else:
                lines.append(" | ".join("" if v is None else str(v) for v in row))
        return "\n".join(lines)

    for statement, parameters in captured:
        sql = _inline(statement, parameters)
        if sql in seen:
            continue
        seen.add(sql)
        try:
            plan = await get_instance(hass).async_add_executor_job(one, sql)
        except Exception as err:  # noqa: BLE001 - a plan is diagnostics, never the point
            plan = f"EXPLAIN FAILED: {err}"
        chunks.append(f"\n=== statement {len(seen)} ===\n{sql}\n--- plan ---\n{plan}\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(chunks))


# --------------------------------------------------------------------- bench


class Bench:
    """Runs each case `run.repeat` times after a warm-up and collects the numbers."""

    def __init__(self, hass, run: Run) -> None:
        self.hass = hass
        self.run_info = run
        self.meter = Meter(hass)
        self.results: list[dict] = []

    async def run(
        self,
        name: str,
        fn: Callable,
        check: Callable | None = None,
        plans: str | None = None,
    ) -> None:
        # The warm-up pass doubles as the EXPLAIN capture when asked.
        self.meter.capture = plans is not None
        self.meter.captured = []
        with self.meter.window():
            warm = await fn()
        self.meter.capture = False
        captured = self.meter.captured
        samples = []
        for _ in range(self.run_info.repeat):
            # Whatever the recorder still owes, it should not land inside
            # the timed window.
            await get_instance(self.hass).async_block_till_done()
            with self.meter.window():
                started = time.perf_counter()
                await fn()
                elapsed = (time.perf_counter() - started) * 1000
            samples.append(
                (elapsed, self.meter.statements, self.meter.statistics, self.meter.rows)
            )
        row = {
            "case": name,
            "median_ms": round(stats.median(s[0] for s in samples), 2),
            "max_ms": round(max(s[0] for s in samples), 2),
            "statements": max(s[1] for s in samples),
            "statistics_statements": max(s[2] for s in samples),
            "rows": max(s[3] for s in samples),
            "check": check(warm) if check else None,
        }
        self.results.append(row)
        print(
            f"{row['case']:<58} {row['median_ms']:>9.2f} {row['max_ms']:>9.2f} "
            f"{row['statements']:>6} {row['statistics_statements']:>6} "
            f"{row['rows']:>8}",
            flush=True,
        )
        if plans is not None and captured:
            await explain(
                self.hass,
                captured,
                Path(self.run_info.results_dir)
                / "plans"
                / f"{plans}-{_slug(name)}.txt",
            )


HEADER = f"{'case':<58} {'median':>9} {'max':>9} {'stmts':>6} {'stat':>6} {'rows':>8}"


def _check_buckets(result: dict) -> dict:
    """Every bucket, as the card would draw it: [start_ms, end_ms, change]."""
    return {
        sid: [[b["start"], b["end"], round(b["change"], 6)] for b in buckets]
        for sid, buckets in sorted(result.items())
    }


def _check_stock(result: dict) -> dict:
    """The stock command's answer, reduced to [start_ts, change] per row."""
    return {
        sid: [
            [
                row["start"],
                round(row["change"], 6) if row.get("change") is not None else None,
            ]
            for row in series
        ]
        for sid, series in sorted(result.items())
    }


# ----------------------------------------------------------------- history_stats


def _history_window(period: str, now: float, tz) -> tuple[float, float]:
    """The window the sensor will read, so history_stats is asked the same one.

    `reading._window` takes the period's bounds as of `now` and `pieces`
    then stops at `now`; history_stats has no watermark, so the clamp has
    to be in the templates.
    """
    start, end = periods.bounds(period, now, tz)
    assert start is not None
    return start, min(end, now)


def _iso(timestamp: float, tz) -> str:
    """A template that renders to exactly this instant, in the instance's zone.

    A literal rather than `now()`: the bench anchors on the database's
    newest compiled hour, and history_stats would otherwise read its own
    clock and a different window.
    """
    return datetime.fromtimestamp(timestamp, tz).isoformat()


def _history_stats(hass, entity_id, states, start, end, tz, duration=None):
    """A cold `HistoryStats` - it caches, so a fresh one per repeat."""
    return HistoryStats(
        hass,
        entity_id,
        list(states),
        None if duration is not None else Template(_iso(start, tz), hass),
        Template(_iso(end, tz), hass),
        duration,
        timedelta(0),
    )


async def _cold_partial(compiler, cfg, frame, sums, hours, partial: Partial):
    """`PeriodCoordinator._partial`, with this run's own caches."""
    if frame.earliest is not None and frame.earliest <= partial.hour:
        if partial.hour not in hours:
            hours[partial.hour] = await compiler.async_tail(
                cfg, partial.hour, partial.hour + HOUR
            )
        if (timeline := hours[partial.hour]) is not None:
            return reading.exact_partial(partial, timeline)
    return reading.prorate(
        partial,
        *reading.hour_change(
            frame.existing,
            lambda sid, edge: sums.get((sid, edge), 0.0),
            partial.hour,
        ),
    )


async def cold_reading(compiler, hass, cfg, spec: Spec, now: float, tz):
    """One sensor's value from nothing cached: frame, edges, part hours, tail.

    What `PeriodCoordinator._async_update_data` does on its first refresh,
    for one sensor - the fair counterpart to a cold `HistoryStats`, which
    also reads everything it needs.
    """
    frame = await frame_of(compiler, hass, cfg.entity_id)
    planned = reading.plan(spec, frame, now, tz)
    sums: dict[tuple[str, float], float] = {}
    if (wanted := reading.edges_of(planned)) and frame.existing:
        at_edges = await get_instance(hass).async_add_executor_job(
            ds_rows.sums_at_edges, hass, set(frame.existing), wanted
        )
        for edge, at_edge in at_edges.items():
            for statistic_id in frame.existing:
                sums[(statistic_id, edge)] = at_edge.get(statistic_id, 0.0)
    partials: dict[Partial, reading.PartialValue] = {}
    hours: dict[float, object] = {}
    for partial in planned.pieces.partials if planned.pieces is not None else ():
        if partial not in partials:
            partials[partial] = await _cold_partial(
                compiler, cfg, frame, sums, hours, partial
            )
    timeline = None
    if (
        spec.live
        and planned.pieces is not None
        and planned.pieces.tail
        and frame.watermark_end is not None
    ):
        timeline = await compiler.async_tail(cfg, frame.watermark_end, now)
    return reading.compute(
        cfg,
        spec,
        frame,
        planned,
        lambda sid, edge: sums.get((sid, edge), 0.0),
        partials.get,
        timeline,
        now,
    )


async def _history_group(bench, hass, compiler, cases, entity_configs, now, tz) -> None:
    """Each pair twice - history_stats then the period sensor - plus its values."""
    for pair in cases.history:
        cfg = entity_configs[pair.entity_id]
        start, end = _history_window(pair.period, now, tz)
        spec = Spec(pair.states, pair.metric, pair.period, True)
        duration = (
            timedelta(seconds=end - start) if pair.period.startswith("last_") else None
        )

        async def hstats(pair=pair, start=start, end=end, duration=duration):
            return await _history_stats(
                hass, pair.entity_id, pair.states, start, end, tz, duration
            ).async_update(None)

        async def psensor(cfg=cfg, spec=spec):
            return await cold_reading(compiler, hass, cfg, spec, now, tz)

        # `Bench.run` hands its warm-up answer to `check`, which is how
        # both sides' values are had without asking a third time.
        answers: dict[str, object] = {}

        def keep(got, key="", answers=answers):
            answers[key] = got

        await bench.run(f"hstats  {pair.name}", hstats, lambda got: keep(got, "hstats"))
        hs = bench.results[-1]
        await bench.run(
            f"psensor {pair.name}", psensor, lambda got: keep(got, "psensor")
        )
        ps = bench.results[-1]

        hs_state = answers["hstats"]
        got = answers["psensor"]
        value = None if got.value is None else float(got.value)
        if pair.kind == "count":
            left, right = float(hs_state.match_count), value
            tolerance = None
        elif pair.kind == "ratio":
            # The sensor's share is over the elapsed part of the period
            # (`reading.compute`), which is exactly `end - start` here
            # because `end` is already clamped to the anchor.
            left, right = (
                round(hs_state.seconds_matched / (end - start) * 100, 1),
                value,
            )
            tolerance = cases.ratio_tolerance
        else:
            left, right = round(hs_state.seconds_matched / HOUR, 4), value
            tolerance = cases.time_tolerance_hours
        check = {
            "kind": pair.kind,
            "window": [start, end],
            "hstats": left,
            "psensor": right,
            "delta": None if right is None else round(left - right, 4),
            "estimated": got.estimated,
            "match_count": hs_state.match_count,
            "seconds_matched": round(hs_state.seconds_matched, 3),
        }
        hs["check"] = ps["check"] = check
        if pair.show_sql:
            for label, fn in (("hstats", hstats), ("psensor", psensor)):
                with bench.meter.window():
                    await fn()
                for text in bench.meter.texts:
                    print(f"sql     {label:<8} {text[:120]}", flush=True)

        note = ""
        if right is None:
            note = "  (the sensor has no value)"
        elif tolerance is None:
            note = "  (counted differently: intervals vs transitions in)"
        elif abs(left - right) > tolerance:
            note = f"  DISAGREE by {left - right:+.4f}"
        print(
            f"value   {pair.name:<50} hstats={left} psensor={right}{note}",
            flush=True,
        )


# --------------------------------------------------------------------- modes


async def measure(hass, cases, run: Run, now: float, tz, ws_client=None) -> Bench:
    """Every read path, timed. Writes nothing to the database."""
    bench = Bench(hass, run)
    compiler = Compiler(hass)
    plans = f"{run.branch}-{run.engine}-{run.variant}"
    print(
        f"\nengine {run.engine} variant {run.variant} "
        f"anchor {datetime.fromtimestamp(now, timezone.utc)} "
        f"our rows {await our_rows(hass)}\n{HEADER}",
        flush=True,
    )

    for case in cases.buckets:
        start, end = now - case.days * DAY, now

        async def one(case=case, start=start, end=end):
            return await get_instance(hass).async_add_executor_job(
                websocket._buckets,
                hass,
                set(case.statistic_ids),
                start,
                end,
                case.period,
            )

        await bench.run(f"buckets {case.name}", one, _check_buckets, plans=plans)

    stock_answers: dict[str, dict] = {}
    for case in cases.buckets:
        if not case.stock:
            continue
        start = datetime.fromtimestamp(now - case.days * DAY, timezone.utc)
        end = datetime.fromtimestamp(now, timezone.utc)

        async def one(case=case, start=start, end=end):
            return await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                start,
                end,
                set(case.statistic_ids),
                case.period,
                None,
                {"change"},
            )

        await bench.run(f"stock   {case.name}", one, _check_stock)
        stock_answers[case.name] = bench.results[-1]["check"]

    # Agreement: wherever the stock command has a bucket, ours must have
    # the same change at the same start; ours may add zeros the stock
    # command omits (a compiled period the state was absent from).
    ours_answers = {
        r["case"][8:]: r["check"]
        for r in bench.results
        if r["case"].startswith("buckets")
    }
    for name, stock in stock_answers.items():
        ours = ours_answers[name]
        agree = disagree = extra = 0
        for sid_, series in stock.items():
            mine = {round(b[0] / 1000): b[2] for b in ours.get(sid_, [])}
            theirs = {
                round(start_ts): change
                for start_ts, change in series
                if change is not None
            }
            for start_ts, change in theirs.items():
                if start_ts in mine and abs(mine[start_ts] - change) < 1e-6:
                    agree += 1
                else:
                    disagree += 1
            extra += sum(1 for k, v in mine.items() if k not in theirs and v == 0.0)
        for r in bench.results:
            if r["case"] == f"buckets {name}":
                r["agreement"] = {
                    "agree": agree,
                    "disagree": disagree,
                    "extra_zero": extra,
                }
        print(
            f"agree   {name:<50} {agree} match, {disagree} differ, {extra} zeros only ours",
            flush=True,
        )

    if ws_client is not None:
        for case in cases.buckets:
            if not case.websocket:
                continue
            start = datetime.fromtimestamp(now - case.days * DAY, timezone.utc)
            end = datetime.fromtimestamp(now, timezone.utc)
            sizes: dict[str, int] = {}

            async def ours_ws(case=case, start=start, end=end, sizes=sizes):
                await ws_client.send_json_auto_id(
                    {
                        "type": "discrete_statistics/buckets",
                        "statistic_ids": list(case.statistic_ids),
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                        "period": case.period,
                    }
                )
                response = await ws_client.receive_json()
                assert response["success"], response
                sizes["ours"] = len(json.dumps(response["result"]))
                return response["result"]

            async def stock_ws(case=case, start=start, end=end, sizes=sizes):
                await ws_client.send_json_auto_id(
                    {
                        "type": "recorder/statistics_during_period",
                        "statistic_ids": list(case.statistic_ids),
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                        "period": case.period,
                        "types": ["change"],
                    }
                )
                response = await ws_client.receive_json()
                assert response["success"], response
                sizes["stock"] = len(json.dumps(response["result"]))
                return response["result"]

            await bench.run(f"ws ours  {case.name}", ours_ws)
            bench.results[-1]["response_bytes"] = sizes["ours"]
            await bench.run(f"ws stock {case.name}", stock_ws)
            bench.results[-1]["response_bytes"] = sizes["stock"]
            print(
                f"bytes   {case.name:<50} ours {sizes['ours']}, stock {sizes['stock']}",
                flush=True,
            )

    entity_configs = {cfg.entity_id: cfg for cfg in configs(run)}
    frames = {}
    for case in cases.sensors:
        if case.entity_id not in frames:
            frames[case.entity_id] = await frame_of(compiler, hass, case.entity_id)

    for case in cases.sensors:
        frame = frames[case.entity_id]
        planned = reading.plan(case.spec, frame, now, tz)
        edges = sorted(reading.edges_of(planned))

        async def one(frame=frame, edges=edges):
            # What `PeriodCoordinator._async_update_data` does with a cold
            # cache: every planned edge in the one read, in the executor.
            sums: dict[tuple[str, float], float] = {}
            if edges and frame.existing:
                at_edges = await get_instance(hass).async_add_executor_job(
                    ds_rows.sums_at_edges, hass, set(frame.existing), set(edges)
                )
                for edge, at in at_edges.items():
                    for statistic_id in frame.existing:
                        sums[(statistic_id, edge)] = at.get(statistic_id, 0.0)
            return sums

        def value(sums, case=case, frame=frame, planned=planned):
            # Anchored on the watermark, so there is no tail and no part
            # hour: the reading is the sums at the two edges.
            got = reading.compute(
                entity_configs[case.entity_id],
                case.spec,
                frame,
                planned,
                lambda sid, edge: sums.get((sid, edge), 0.0),
                lambda partial: None,
                None,
                now,
            )
            return {
                "value": got.value,
                "start": got.period_start,
                "end": got.period_end,
                "estimated": got.estimated,
                "reason": got.reason,
                "sums": {
                    f"{sid}@{int(edge)}": round(v, 6)
                    for (sid, edge), v in sorted(sums.items())
                },
            }

        await bench.run(f"sensor  {case.name} ({len(edges)} edges)", one, value)

    if cases.history:
        await _history_group(bench, hass, compiler, cases, entity_configs, now, tz)

    wanted = cases.entities or tuple(entity_configs)
    for entity_id in wanted:

        async def one(entity_id=entity_id):
            return await compiler.async_compiled(entity_id)

        await bench.run(
            f"frame   {entity_id}",
            one,
            lambda got: {"watermark": got[1], "statistics": sorted(got[0])},
        )

    return bench


async def build(hass, run: Run, cases=None) -> None:
    """Compile every entry from its earliest retained state. Writes.

    `cases.entities`, when it names any, restricts the run to those - a
    profile of two entities out of fourteen is the same measurement and a
    fraction of the wall clock.
    """
    end = sqlite_anchor(run.build_anchor) if run.build_anchor else None
    where = "now" if end is None else str(datetime.fromtimestamp(end, timezone.utc))
    print(f"\nbuilding {run.engine}/{run.variant} to {where}", flush=True)
    compiler = Compiler(hass)
    started = time.perf_counter()
    with profiler_for(hass, run) as profiler:
        for cfg in wanted_configs(run, cases):
            one = time.perf_counter()
            with entity_profile(profiler, cfg.entity_id):
                hours = await compiler.async_compile(cfg, None, end)
            print(
                f"{cfg.entity_id:<50} {hours:>7} hours "
                f"{time.perf_counter() - one:>9.1f}s "
                f"rows {await our_rows(hass):>9}",
                flush=True,
            )
    print(
        f"built in {time.perf_counter() - started:.1f}s; "
        f"our rows now {await our_rows(hass)}",
        flush=True,
    )


async def compile_(hass, run: Run, end: float, cases=None) -> Bench:
    """The write path: a trailing-window compile, and one day recompiled."""
    bench = Bench(hass, run)
    compiler = Compiler(hass)
    print(f"\n{HEADER}", flush=True)
    with profiler_for(hass, run) as profiler:
        for cfg in wanted_configs(run, cases):

            async def one(cfg=cfg):
                return await compiler.async_compile_incremental(cfg)

            with entity_profile(profiler, f"incremental-{cfg.entity_id}"):
                await bench.run(f"incremental {cfg.entity_id}", one, lambda got: got)

        for cfg in wanted_configs(run, cases):

            async def one(cfg=cfg, end=end):
                return await compiler.async_compile(cfg, end - DAY, end)

            with entity_profile(profiler, f"one-day-{cfg.entity_id}"):
                await bench.run(f"one day     {cfg.entity_id}", one, lambda got: got)

    return bench


def _results_dir(results_dir: str) -> Path:
    """Where result JSON files land.

    `results_dir` holds `results/`, `plans/` and `run.json` side by side -
    except when it is itself already named "results" (the default,
    `bench/results`), in which case it *is* that directory, so nesting a
    second `results/` inside it would bury the files a level deeper than
    every other path (`plans/`, `run.json`) already sits.
    """
    base = Path(results_dir)
    return base if base.name == "results" else base / "results"


async def write_results(bench: Bench, hass, run: Run, url: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # The mode is in the name for every mode but `measure`, so a compile
    # run is never mistaken for the read set it sits beside.
    tag = "" if run.mode == "measure" else f"-{run.mode}"
    out = (
        _results_dir(run.results_dir)
        / f"{run.branch}-{run.engine}-{run.variant}{tag}-{stamp}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                **{k: v for k, v in asdict(run).items() if k != "results_dir"},
                "ha_version": HA_VERSION,
                "db_url": url,
                "our_statistics_rows": await our_rows(hass),
                "results": bench.results,
            },
            indent=1,
        )
    )
    print(f"\nwrote {out}", flush=True)
    return out
