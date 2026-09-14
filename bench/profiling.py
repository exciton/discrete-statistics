"""One entity's compile, profiled and split into phases. Harness only.

Opt-in: `BENCH_PROFILE=1` travels in `run.json` as `profile`, and the
`build` and `compile` modes then wrap each entity in `session()`. The
integration is untouched - every measurement here is a wrapper the
harness installs around the compiler's own module attributes and takes
off again.

Two instruments, because one is not enough:

* `cProfile`, on the event loop thread. It sees the Python the loop runs
  - canonicalise, bucket, payload - and nothing of what an executor job
  does, because a profiler is per-thread and a coroutine's frame is off
  the stack while it awaits. So its totals answer "which Python is
  expensive", not "where did the wall clock go".
* The phase split, which does answer that. Each phase is a function the
  compile calls, wrapped to time itself *in whichever thread it runs in*;
  SQLAlchemy's cursor events attribute every statement and its elapsed
  time to the phase that thread is currently in. A statement issued while
  no phase is current is the recorder's own - the import tasks'
  SELECT-exists and INSERT/UPDATE per row, on the recorder thread - and
  is collected under `recorder`.
"""

from __future__ import annotations

import cProfile
import io
import pstats
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import history as history_module
from homeassistant.components.recorder.core import Recorder
from sqlalchemy import event as sqlalchemy_event

from custom_components.discrete_statistics import compiler as compiler_module

_STATISTICS = re.compile(r"\bstatistics\b", re.IGNORECASE)

# The phase a thread is inside, if any. Thread-local because the executor
# threads and the loop thread are in different phases at the same moment.
_current = threading.local()


@dataclass
class Phase:
    calls: int = 0
    seconds: float = 0.0
    # Enqueue only: the StatisticData rows handed over, and the calls that
    # handed over none - a metadata-only import, which still costs the
    # recorder a task and a commit.
    rows: int = 0
    empty: int = 0
    statements: int = 0
    statistics_statements: int = 0
    sql_seconds: float = 0.0


#: What each phase is, for the report's legend: the function wrapped, and
#: the thread it runs in.
LEGEND = {
    "earliest": "compiler.state_changes_during_period(limit=1) - the oldest retained state [executor]",
    "metadata": "compiler.get_metadata - statistics_meta, per compile and per chunk [executor]",
    "watermark": "rows.series_end - the newest compiled hour [executor]",
    "base": "rows.bases - the sums the window continues from [executor]",
    "history": "compiler.state_changes_during_period - a chunk's rows [executor]",
    "history: query+fetch": "  of which recorder.history.execute_stmt_lambda_element - the statement and its rows",
    "history: State objects": "  of which recorder.history._sorted_states_to_dict - the State objects built from them",
    "canonicalise": "canonicalise.canonicalise - rows -> transitions [loop]",
    "bucket": "bucketer.bucket - transitions -> per (state, hour) [loop]",
    "standing": "rows.standing - the rows already written in the chunk [executor]",
    "payload": "payload.build_payloads - buckets -> StatisticData rows [loop]",
    "enqueue": "async_add_external_statistics - the call itself, one per statistic [loop]",
    "drain": "Recorder.async_block_till_done - waiting for the queue [loop]",
    "recorder": "no phase current: the recorder thread's own work on the import tasks",
}


#: Phases measured inside another one. Counted once in the total, and
#: shown indented so the table reads as the split it is.
NESTED = ("history: query+fetch", "history: State objects")


class Timers:
    """Per-phase totals, written from every thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.phases: dict[str, Phase] = {}

    def reset(self) -> None:
        with self.lock:
            self.phases = {}

    def _phase(self, name: str) -> Phase:
        return self.phases.setdefault(name, Phase())

    def add(self, name: str, seconds: float) -> None:
        with self.lock:
            phase = self._phase(name)
            phase.calls += 1
            phase.seconds += seconds

    def sql(self, name: str, seconds: float, statistics: bool) -> None:
        with self.lock:
            phase = self._phase(name)
            phase.statements += 1
            phase.statistics_statements += int(statistics)
            phase.sql_seconds += seconds

    def enqueued(self, name: str, rows: int) -> None:
        with self.lock:
            phase = self._phase(name)
            phase.rows += rows
            phase.empty += int(not rows)

    @contextmanager
    def phase(self, name: str):
        previous = getattr(_current, "name", None)
        _current.name = name
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - started)
            _current.name = previous


def _sync(timers: Timers, name: str, fn):
    def wrapper(*args, **kwargs):
        with timers.phase(name):
            return fn(*args, **kwargs)

    return wrapper


def _async(timers: Timers, name: str, fn):
    async def wrapper(*args, **kwargs):
        with timers.phase(name):
            return await fn(*args, **kwargs)

    return wrapper


def _enqueue(timers: Timers, fn):
    """`async_add_external_statistics(hass, metadata, rows)`, counted.

    How many rows a compile hands the recorder, and how many of the calls
    hand it none at all, is the size of the write path - which the drain
    does not show, because the recorder keeps up with it as it goes.
    """

    def wrapper(hass, metadata, rows, *args, **kwargs):
        timers.enqueued("enqueue", len(rows))
        with timers.phase("enqueue"):
            return fn(hass, metadata, rows, *args, **kwargs)

    return wrapper


def _history(timers: Timers, fn):
    """`state_changes_during_period` serves two questions.

    The earliest-retained-state lookup passes `limit=1` and is one call
    per compile; a chunk's read passes none and is the bulk. Timing them
    as one phase would hide whichever is the smaller.
    """

    def wrapper(*args, **kwargs):
        limit = args[6] if len(args) > 6 else kwargs.get("limit")
        with timers.phase("earliest" if limit == 1 else "history"):
            return fn(*args, **kwargs)

    return wrapper


class Profiler:
    """Installs the wrappers and the cursor events for the run's lifetime."""

    def __init__(self, hass, out_dir: Path, label: str) -> None:
        self.hass = hass
        self.out_dir = Path(out_dir)
        self.label = label
        self.timers = Timers()
        self._started = threading.local()
        engine = get_instance(hass).engine
        sqlalchemy_event.listen(engine, "before_cursor_execute", self._before)
        sqlalchemy_event.listen(engine, "after_cursor_execute", self._after)
        self._originals: list[tuple[object, str, object]] = []
        self._install()

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        self._started.at = time.perf_counter()
        self._started.statistics = bool(_STATISTICS.search(statement))

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        started = getattr(self._started, "at", None)
        if started is None:
            return
        self.timers.sql(
            getattr(_current, "name", None) or "recorder",
            time.perf_counter() - started,
            getattr(self._started, "statistics", False),
        )
        self._started.at = None

    def _patch(self, target, name: str, factory) -> None:
        original = getattr(target, name)
        self._originals.append((target, name, original))
        setattr(target, name, factory(original))

    def _install(self) -> None:
        timers = self.timers
        module = compiler_module
        self._patch(module, "state_changes_during_period", lambda f: _history(timers, f))
        # Inside the history read, which is the phase that matters: the
        # statement and its rows, and then the `State` objects built from
        # them. Both are the recorder's own module-level names, so
        # patching them here reaches the call `state_changes_during_period`
        # makes without the integration seeing anything.
        self._patch(
            history_module,
            "execute_stmt_lambda_element",
            lambda f: _sync(timers, "history: query+fetch", f),
        )
        self._patch(
            history_module,
            "_sorted_states_to_dict",
            lambda f: _sync(timers, "history: State objects", f),
        )
        self._patch(module, "get_metadata", lambda f: _sync(timers, "metadata", f))
        self._patch(module, "series_end", lambda f: _sync(timers, "watermark", f))
        self._patch(module, "bases", lambda f: _sync(timers, "base", f))
        self._patch(module, "standing", lambda f: _sync(timers, "standing", f))
        self._patch(module, "canonicalise", lambda f: _sync(timers, "canonicalise", f))
        self._patch(module, "bucket", lambda f: _sync(timers, "bucket", f))
        self._patch(module, "build_payloads", lambda f: _sync(timers, "payload", f))
        self._patch(
            module,
            "async_add_external_statistics",
            lambda f: _enqueue(timers, f),
        )
        self._patch(
            Recorder, "async_block_till_done", lambda f: _async(timers, "drain", f)
        )

    def close(self) -> None:
        for target, name, original in reversed(self._originals):
            setattr(target, name, original)
        self._originals = []

    @contextmanager
    def entity(self, entity_id: str):
        """Profile one entity's compile and write its report."""
        self.timers.reset()
        profile = cProfile.Profile()
        started = time.perf_counter()
        profile.enable()
        try:
            yield
        finally:
            profile.disable()
            elapsed = time.perf_counter() - started
            self.write(entity_id, elapsed, profile)

    def write(self, entity_id: str, elapsed: float, profile: cProfile.Profile) -> Path:
        out = self.out_dir / f"{self.label}-{entity_id.replace('.', '_')}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f"{entity_id}  {self.label}\n"
            f"wall {elapsed:.2f}s  chunk_hours {compiler_module.CHUNK_HOURS}\n\n"
            f"{self.table(elapsed)}\n\n{self.top(profile)}"
        )
        return out

    def table(self, elapsed: float) -> str:
        """The per-phase split as a small markdown table."""
        rows = [
            "| phase | calls | seconds | % wall | stmts | on statistics | sql s |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        order = list(LEGEND)
        with self.timers.lock:
            phases = dict(self.timers.phases)
        accounted = 0.0
        for name in sorted(phases, key=lambda n: (order.index(n) if n in order else 99)):
            phase = phases[name]
            # The loop's own phases are the only ones that are a share of
            # the wall clock; an executor phase overlaps nothing here
            # either, since the compile awaits it - but `recorder` does,
            # and is left out of the total for that reason.
            if name != "recorder" and name not in NESTED:
                accounted += phase.seconds
            rows.append(
                f"| {name} | {phase.calls} | {phase.seconds:.2f} | "
                f"{100 * phase.seconds / elapsed:.1f}% | {phase.statements} | "
                f"{phase.statistics_statements} | {phase.sql_seconds:.2f} |"
            )
        rows.append(
            f"| (unaccounted) |  | {elapsed - accounted:.2f} | "
            f"{100 * (elapsed - accounted) / elapsed:.1f}% |  |  |  |"
        )
        enqueue = phases.get("enqueue", Phase())
        written = (
            f"\nenqueued {enqueue.rows} statistic rows in {enqueue.calls} imports, "
            f"{enqueue.empty} of them metadata only (no rows)\n"
        )
        legend = "\n".join(f"  {name}: {what}" for name, what in LEGEND.items())
        return "\n".join(rows) + written + "\nphases:\n" + legend

    @staticmethod
    def top(profile: cProfile.Profile, count: int = 40) -> str:
        """The profile's own top functions by cumulative time.

        Event loop thread only - see the module docstring.
        """
        buffer = io.StringIO()
        stats = pstats.Stats(profile, stream=buffer)
        stats.sort_stats("cumulative").print_stats(count)
        return buffer.getvalue()
