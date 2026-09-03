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

CI (`.github/workflows/validate.yml`) runs the suite through the same
`script/test`, plus HACS and hassfest. hassfest validates `manifest.json`,
`strings.json` and `translations/`, and it can be run locally against a Home
Assistant core checkout (`/home/bonne/Code/home_assistant_core`, kept on the
pinned tag) rather than waiting for a push:

```bash
docker run --rm -v "$PWD:/workspace" -v /home/bonne/Code/home_assistant_core:/core \
  -w /core ha-discrete-stats-test bash -c \
  "pip install -q ruff; python -m script.hassfest \
   --integration-path /workspace/custom_components/discrete_statistics --action validate"
```

hassfest needs `ruff`, which the test image does not carry. The HACS check
still has no local equivalent.

## Architecture

A pure pipeline with a single I/O boundary. Dependencies point one way:

```
const ─┬─ bucketer          pure: transitions -> {(state, hour): (seconds, count)}
       ├─ statistic_ids     pure: build, parse and match an external statistic ID
       ├─ config ── canonicalise   pure: recorder rows -> canonical transitions
       │   │    └─ config_flow    HA UI: entity -> EntityConfig, as a helper
       │   └─ statistic_ids       for the reserved-token comparison
       └─ payload           pure: buckets -> cumulative StatisticData rows
                │
            compiler        the only module that touches the recorder
                │
            __init__        setup, hourly schedule, recompute service
```

Everything except `compiler` and `config_flow` is pure and testable without
a `hass` instance. Keep it that way: if a change needs recorder access in a
lower module, the design is drifting.

States in a statistic's name are rendered through `async_translate_state`, so
a door sensor reads `Open`/`Closed` as it does everywhere else. It answers with
the raw state when no translation exists, which covers `no_data` and most enum
sensors. `_async_warm_translations` loads the cache first, because that
function is a callback over one and a cold cache would rename every statistic
back and forth. The language is `hass.config.language` — instance-wide, while
the frontend translates per user — so the stored name is in one language for
everyone. That is a known limitation of putting a rendered string in metadata,
not something to fix here.

Statistic display names are resolved in `compiler._display_name`, not in
`payload`: a typed name, then the entity registry, then the live state's
`friendly_name`, then the entity ID. The registry comes before the state
because attributes are stripped while an entity is unavailable — reading the
state first would rename every statistic to the ID for the duration and back
afterwards — and because it holds what the user asked for when the two
disagree.

`Compiler` is a class built from `(hass,)`; the entry points are its methods,
not module-level functions.
`Compiler.async_compile(cfg, start, end=None)` compiles `[start, end)` and is
the single implementation behind live compilation, catch-up after downtime, and
backfill. They differ only in the start timestamp. Preserve that — it is what
makes all three paths share one set of tests.

The hourly run does not call it directly: it calls
`async_compile_incremental`, which is only watermark arithmetic
(`watermark - (TRAILING_HOURS - 1) * HOUR`, or the entity's earliest state when
there is no watermark) before delegating. Keep the derivation of `start` there
and the compiling in `async_compile`. The `recompute` service calls
`async_compile` directly, because the whole point of its `start:` is to
override that arithmetic — do not "tidy" it onto the incremental path.

Configuration arrives from two sources. `hass.data[DOMAIN]["yaml_configs"]`
is static; `["entry_configs"]` holds one `EntityConfig` per config entry;
`["all_configs"]()` joins them, YAML first, and is what the hourly run and
the `recompute` service iterate. The compiler, lock and hourly timer stay
singletons built in `async_setup` — a per-entry timer would let
two entities compile concurrently and defeat the lock.

## Invariants

These look like details and are not. Each exists because its absence produced
wrong data.

**Durations sum to wall-clock time.** Every state's duration for a period must
total exactly the period length: 1.0 per hour, 24 per day. This is the single
best end-to-end check, and it catches both over- and under-attribution.

**Values are cumulative monotonic sums.** Charts use `stat_types: change` to
derive per-bucket values. A sum that decreases is always a bug.

**Rows are dense over every *known* statistic, not merely the states seen in
the window.** `payload.build_payloads` takes `existing`, which `compiler`
sources from the recorder's own `statistics_meta`. Emitting only the window's
own states leaves a statistic with no row in the hour before the next window,
so its cumulative base reads as zero and the series restarts — the sum goes down, and the loss
is permanent because the next run bases on the deflated rows.

Density is also what makes the `mean` correct. Every row carries the hour's
own value as its `mean`, `min` and `max` so the recorder's `_reduce_statistics`
can roll the hours up into an average hourly duration or count. It skips rows
whose mean is `None`, so a sparse hour would not read as a quiet one — it
would drop out of the average entirely and inflate it.

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

**A boundary state that resolves to nothing widens the lookback
(`WIDENING_LOOKBACKS`).** `include_start_time_state` returns exactly *one* row
before the boundary, so a single ignored row there hides the perfectly good
state behind it and the whole span falls to `no_data`. Because `window_start`
moves with the watermark on every run, the idempotent upsert then rewrites
already-correct duration rows downward, permanently. `compiler` widens the
lookback until a recordable state appears; the two legitimate `no_data` cases
(an entity's pre-history, a gap whose rows were purged) still fall through.

**A long compile is chunked, and the base sum is threaded through it.**
`async_compile` walks the window in `CHUNK_HOURS` slices to bound memory during
a backfill, and each slice returns the base sums the next one starts from.
Re-reading the base per chunk instead would break monotonicity in exactly the
way the density invariant describes, because the recorder writes are still
queued.

**A scheduled run is skipped, not queued, when the recorder is behind.**
`__init__` compares `get_instance(hass).backlog` against `BACKLOG_THRESHOLD`
and returns early. This is safe only because the watermark is data-derived: a
skipped run costs latency and nothing else, and the next run picks up the same
hours. Do not add catch-up bookkeeping for it.

**Windows are half-open, `[window_start, window_end)`.** `canonicalise` routes
rows *strictly* before `window_start` into the carried state; `bucketer` skips
transitions *strictly* before it. Together they tile the timeline with no
overlap or hole, so a transition exactly on a boundary is counted exactly once
regardless of which window compiles it. Changing either comparison alone
silently loses or double-counts boundary events.

**A statistic ID's state is exactly one token, and that is what makes it
readable.** `build` slugifies the state with *no* separator, so an ID is
`<entity slug>_<state token>_<metric>` and `parse` reads it back from the
right: last token the metric, second-to-last the state, everything before the
entity. Without that, `climate.zone` in state `heat_cool` and
`climate.zone_heat` in state `cool` produce the *same* ID and interleave
two entities' data in one series — `VALID_STATISTIC_ID` allows only
`[a-z0-9_]` and forbids `__`, so no separator can be reserved to tell them
apart. There is no stored index: `belongs_to` plus the recorder's
`statistics_meta` is the whole association, which means a statistic and its
description can never drift apart.

**Two states with the same token are one statistic, and must be added.**
`payload._fold` groups buckets by `state_token` before building rows.
`heat_cool` and `heatcool` merge, deliberately — it behaves like a free state
map. Keying payloads by raw state instead would let the second state's rows
*replace* the first's, and the hour would stop totalling wall-clock time.

**A statistic not seen in a window is still relabelled.** `build_payloads`
receives each existing statistic's stored name and swaps the display half via
`payload.rename`, splitting on the *last* `": "`. The state half cannot be
rebuilt from the ID — the ID holds only the token — so a rename would
otherwise never reach a state the entity has not been in for months, and
neither would a change to `mean_type` or the units.

**`no_data` is reserved, but it can be chosen.** It is what the compiler
attributes a span to when it cannot determine a real state — before an entity's
first known state, or across a gap whose source rows were purged. A config may
also name it, as `blank: no_data` or as a `states` target, which is
how an operator says "I cannot interpret this, chart it as a gap".

What stays forbidden is a device reaching the band *by itself*: a raw state
that happens to be called `no_data` resolves to nothing instead, or the band
would stop distinguishing "the device said this" from "we could not tell".
`resolve` enforces that with the `chosen` flag — the test is who asked, not
what the value is.

Either way it has a duration statistic and no count. Transitions into it do
exist once a config routes states there, so the absence is now a deliberate
choice rather than a structural certainty: it is a band for spans we cannot
describe, and counting them would measure our own ignorance.

**A blank state is substituted, before anything else looks at it.** A blank
state is one with no letters or digits, so it has no name a statistic could
carry. Judged on the input rather than the token: `slugify` answers the literal
`"unknown"` for punctuation, whitespace and emoji alike, so a state that
genuinely spells unknown once normalised — `__unknown__`, `Unknown!` — is a
real name and merges with `unknown` exactly as `heat_cool` merges with
`heatcool`. `resolve` applies `cfg.blank`
— `ignore` to carry forward, otherwise a name swapped in and then resolved
normally, so it inherits a real state's disposition rather than
needing a rule of its own. Substitute-then-resolve, not a direct answer: a
direct answer would bypass `default` and make the stock `unknown` record where
it used to be ignored. An explicit `states` entry for the raw value wins over
the substitution — blank is the most meaningful state a text error sensor has,
and only the config knows that. The recorder stores NULL when an entity is removed
or reloaded, and the history API hands that back as `""` — which tokenises to
nothing and would leave a double underscore. It is not an absence of data,
which is what `no_data` means; it is a state we cannot name. Letting it reach
`build` instead raises `InvalidStatisticIdError`, and that aborted the whole
entity's compile — permanently, because the watermark never advanced past the
chunk containing it.

**A series never opens with `no_data`.** An entity's first state rarely lands
on the hour, so compiling from the hour containing it would give every helper
a `no_data` statistic recording the minutes before it — and by the density
invariant that statistic is then written forever. `_async_compile_chunk`
advances `window_start` to the first whole hour that begins in a recordable
state instead. The trim is conditional on the entity having no statistics at
all, and must stay that way: once statistics exist, skipping hours leaves a
hole, and the next run finds no base in the hour before its window and restarts
every cumulative sum at zero.

**Nothing in this integration deletes statistics.** Recompute overwrites
buckets it has source data for and leaves everything else alone, so a rebuild
can never discard statistics whose source states have already been purged.
Deleting a statistic is the user's decision, made in Settings → System → Tools
→ Statistics — and it sticks, because nothing else records that the statistic
existed. It is absent from `statistics_meta` on the next compile, so it is
absent from `existing` and is never written again. A state that *recurs* is
recorded in full, both metrics of it; only the metrics of states that do not
occur stay deleted.

**An entity is configured once, from one source.** Two configurations
resolve the same raw states through different disposition tables and write
conflicting values to the same statistic IDs. The flow refuses an entity
YAML owns; `async_setup_entry` raises `ConfigEntryError` when YAML claims
one an entry already owns, which is also what keeps it out of
`all_configs()`. YAML wins, because `async_setup` runs first.

## Home Assistant APIs, and their traps

Verified against 2026.8.3. Each of these was got wrong once.

- `async_add_external_statistics` is a `@callback` — call it, do not await it.
  It only *enqueues*; `Compiler.async_compile` drains via `async_block_till_done()`
  before returning so a subsequent compile reads a base including those writes.
- Every other recorder query is synchronous and must run through
  `get_instance(hass).async_add_executor_job(...)`.
- `include_start_time_state=True` does **not** return the state as of
  `start_time`. Both underlying queries use strict comparisons, so a row landing
  exactly on the boundary is returned by neither. `compiler` queries from
  `window_start - START_MARGIN` to compensate. The margin is deliberately not
  an integer number of seconds, and `math.nextafter` cannot be used — a 1-ULP
  offset at epoch scale is below `datetime`'s microsecond resolution and rounds
  straight back.
- `StatisticsRow["start"]` is a `float` timestamp, not a `datetime`.
- A statistic may carry a sum *and* a mean. `mean_type` and `has_sum` are
  independent fields and nothing in the import path rejects the combination,
  so one statistic serves both `stat_types: change` and `stat_types: mean`.
  `has_mean` is deprecated but still a real column, and
  `StatisticsMeta.from_meta` passes the metadata dict through verbatim — keep
  it consistent with `mean_type` rather than leaving it stale.
- Changing metadata is picked up: `StatisticsMetaManager._update_metadata`
  compares `mean_type` and rewrites the row, so an existing statistic gains a
  mean on the next compile. Its already-written rows keep a `NULL` mean until
  they are recompiled.
- The recorder's upsert on `(metadata_id, start_ts)` is what makes recompilation
  idempotent. It holds only when recomputation starts from a bucket whose base
  sum is known, which is why the base is read from the bucket *preceding* the
  window rather than the newest one.
- `Compiler.async_compile` drains the recorder in a `finally`, not on the
  success path. A chunk that raises leaves earlier chunks' writes queued, and
  density is now read live from `statistics_meta` — so the next compile could
  see half of them and leave the rest sparse.
- The two `_async_existing` reads in an incremental compile are not
  redundant. Reusing the first one — taken before `_async_watermark`'s
  round-trips — makes a recently deleted statistic intermittently still
  visible, and the deletion tests flaky about one run in three.
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
