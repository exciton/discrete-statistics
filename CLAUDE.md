# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A Home Assistant custom integration that records per-state transition counts
and durations for binary and enum entities as external long-term statistics,
retained independently of `purge_keep_days`. See README.md for user-facing
configuration and chart examples.

## Commands

Tests run in a container. Home Assistant 2026.8.3 requires Python >= 3.14.2,
which is newer than most hosts' system Python, so `script/test` builds
`.devcontainer/Dockerfile` on first use and passes its arguments to pytest.
**Never invoke `pytest` directly** — it will fail or, worse, run against a
different Home Assistant version.

```bash
script/test tests/                              # whole suite
script/test tests/test_compiler.py -v           # one file
script/test tests/test_compiler.py::test_name   # one test
```

`pytest.ini` and `conftest.py` both live at the repository root. `conftest.py`
must stay there: declaring `pytest_plugins` in a non-rootdir conftest is an
error in modern pytest and breaks the entire suite.

## Architecture

A pure pipeline with a single I/O boundary. Dependencies point one way:

```
const ─┬─ bucketer          pure: transitions -> {(state, hour): (seconds, count)}
       ├─ statistic_ids     pure: build an external statistic ID
       ├─ config ── canonicalise   pure: recorder rows -> canonical transitions
       ├─ registry          .storage: statistic_id -> (entity, state, metric)
       └─ payload           pure: buckets -> cumulative StatisticData rows
                │
            compiler        the only module that touches the recorder
                │
            __init__        setup, hourly schedule, recompute service
```

Everything except `compiler` and `registry` is pure and testable without a
`hass` instance. Keep it that way: if a change needs recorder access in a lower
module, the design is drifting.

`compiler.async_compile(cfg, start, end=None)` is the single entry point for
live compilation, catch-up after downtime, and backfill. They differ only in
the start timestamp. Preserve that — it is what makes all three paths share
one set of tests.

## Invariants

These look like details and are not. Each exists because its absence produced
wrong data.

**Durations sum to wall-clock time.** Every state's duration for a period must
total exactly the period length: 1.0 per hour, 24 per day. This is the single
best end-to-end check, and it catches both over- and under-attribution.

**Values are cumulative monotonic sums.** Charts use `stat_types: change` to
derive per-bucket values. A sum that decreases is always a bug.

**Rows are dense over every *known* statistic, not merely the states seen in
the window.** `payload.build_payloads` takes `known_states`, which `compiler`
sources from the registry. Emitting only the window's own states leaves a
statistic with no row in the hour before the next window, so its cumulative
base reads as zero and the series restarts — the sum goes down, and the loss
is permanent because the next run bases on the deflated rows.

**The watermark is the max across all of an entity's statistic IDs.** Reading a
single representative ID is wrong: density is only guaranteed from a state's
first appearance, so any one ID can lag arbitrarily.

**Each run recomputes a trailing window (`TRAILING_HOURS`).** The recorder is a
write queue, so a state change late in an hour may be committed after that hour
was first compiled. Compiling each hour exactly once would lose it permanently.
A fixed settling delay does not work — a backlogged recorder exceeds any
threshold.

**Only completed hours are emitted.** Writing a partial bucket that a later run
revises would make cumulative sums briefly wrong.

**Windows are half-open, `[window_start, window_end)`.** `canonicalise` routes
rows *strictly* before `window_start` into the carried state; `bucketer` skips
transitions *strictly* before it. Together they tile the timeline with no
overlap or hole, so a transition exactly on a boundary is counted exactly once
regardless of which window compiles it. Changing either comparison alone
silently loses or double-counts boundary events.

**Statistic IDs are write-only.** `VALID_STATISTIC_ID` forbids double
underscores and allows only `[a-z0-9_]`, so no separator is reserved and IDs
cannot be parsed back unambiguously. `registry` is the only reverse mapping.

**`no_data` is reserved.** It is what the compiler attributes a span to when it
cannot determine a real state — before an entity's first known state, or across
a gap whose source rows were purged. It has a duration statistic but no count:
nothing ever transitions *into* it, so a count would be structurally zero.

**Recompute never deletes.** It overwrites buckets it has source data for and
leaves everything else alone, so a rebuild can never discard statistics whose
source states have already been purged. A `clear` option existed and was
removed for exactly this reason. Deleting a statistic is the user's decision,
made in Settings → System → Tools → Statistics.

## Home Assistant APIs, and their traps

Verified against 2026.8.3. Each of these was got wrong once.

- `async_add_external_statistics` is a `@callback` — call it, do not await it.
  It only *enqueues*; `async_compile` drains via `async_block_till_done()`
  before returning so a subsequent compile reads a base including those writes.
- Every other recorder query is synchronous and must run through
  `get_instance(hass).async_add_executor_job(...)`.
- `clear_statistics` is the exception: it mutates metadata and asserts it is on
  the recorder's own thread, so it must be queued with
  `Recorder.async_clear_statistics`, not dispatched to the db executor.
- `include_start_time_state=True` does **not** return the state as of
  `start_time`. Both underlying queries use strict comparisons, so a row landing
  exactly on the boundary is returned by neither. `compiler` queries from
  `window_start - START_MARGIN` to compensate. The margin is deliberately not
  an integer number of seconds, and `math.nextafter` cannot be used — a 1-ULP
  offset at epoch scale is below `datetime`'s microsecond resolution and rounds
  straight back.
- `StatisticsRow["start"]` is a `float` timestamp, not a `datetime`.
- The recorder's upsert on `(metadata_id, start_ts)` is what makes recompilation
  idempotent. It holds only when recomputation starts from a bucket whose base
  sum is known, which is why the base is read from the bucket *preceding* the
  window rather than the newest one.
- An `asyncio.Lock` in `hass.data` serialises the hourly run against the
  service. It is not reentrant: never call `compile_all` from inside it.

## Units

Durations are stored in **hours** (`unit_of_measurement: "h"`,
`unit_class: "duration"`). The bucketer works in seconds because that is what
timestamp arithmetic yields; `payload` converts once. The stored unit is what
charts display — the statistics-graph card's `unit` option only normalises
across statistics that already carry that unit, it cannot convert on demand.

## Testing conventions

Every test must fail when its fix is reverted. Two tests in this repo's history
passed regardless of the code under test and had to be discarded. When adding a
test for a bug fix, revert the fix, watch it fail, then restore.

Integration tests use `recorder_mock` and `freezer`. `tests/test_compiler.py`,
`test_init.py` and `test_service.py` each override the root conftest's autouse
`auto_enable_custom_integrations` fixture to request `recorder_db_url` first;
this is fixture ordering against `recorder_mock`, not a workaround.
