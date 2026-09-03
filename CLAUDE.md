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
       │   │    └─ config_flow    HA UI: entity -> EntityConfig, per entry
       │   └─ statistic_ids       for the blank-state test
       ├─ naming            HA: entity, state -> the names a person recognises
       └─ payload           pure: buckets -> cumulative StatisticData rows
                │
            compiler        the only module that touches the recorder
                │
            __init__        setup, hourly schedule, recompute service
```

Everything except `compiler`, `config_flow` and `naming` is pure and testable
without a `hass` instance. Keep it that way: if a change needs recorder access in a
lower module, the design is drifting.

States in a statistic's name are rendered by `naming.state_translator`,
which wraps `async_translate_state`, so
a door sensor reads `Open`/`Closed` as it does everywhere else. It answers with
the raw state when no translation exists, which covers most enum sensors.
`_UNRENDERED_STATES` covers the two it cannot name: `unavailable` and
`unknown`, which it returns untouched (`translation.py:469`) because the
frontend renders those from its own `state.default` strings that the backend
never sees. English only.
`naming.async_warm_state_translations` loads the cache first, because that
function is a callback over one and a cold cache would rename every statistic
back and forth. Both live in `naming` rather than `compiler`: they are the
same question as `display_name` asked of a state, and they need `hass` but
never the recorder. The language is `hass.config.language` — instance-wide, while
the frontend translates per user — so the stored name is in one language for
everyone. That is a known limitation of putting a rendered string in metadata,
not something to fix here.

The options dialog's heading is a markdown link to the entity's history. The
entry row's own `...` menu is fixed in the frontend
(`ha-config-entry-row.ts`) — Devices, Entities, Logs, Reload, Rename,
Disable, Delete — and an integration can contribute only an options flow, a
reconfigure flow and a diagnostics download, so the dialog is the only place
a link can go. `description_placeholders` are rendered by `ha-markdown`,
which leaves same-host anchors alone so they navigate in-app.

`naming` answers "what is this entity called": `display_name` for a label,
`describe` for a label plus the entity ID where certainty matters — the
options dialog, notifications. The chain is: a
typed name, then the entity registry, then the live state's `friendly_name`,
then the entity ID. It backs the chart labels; `describe` adds the entity ID for the entry's
title, the options dialog's heading and the notifications, where two
similarly-named entities have to be told apart — those disagreeing is what the entity ID
leaking into a title looked like. The registry comes before the state because
attributes are stripped while an entity is unavailable, and because it holds
what the user asked for when the two disagree.

No `integration_type` in the manifest, deliberately. It reaches one thing:
the heading over the entries on the integration's page, which the frontend
picks from a fixed table keyed on the type
(`ha-config-integration-page.ts:844`) — `Services`, `Hubs`, `Devices`,
`Helpers`, `Hardware`, or `Integration entries` with no type. There is no
custom string; `hass.localize` runs on a frontend key, so `strings.json`
cannot reach it. None of the six describes something that computes over
other entities, so the generic heading is the honest one. Only `helper` and
`entity` are special-cased anywhere else in the frontend, so the key costs
nothing else either way.

**The integration provides no entities.** `integration_type: helper` would
list every entry in the Helpers panel — where `ha-config-helpers.ts:503-546`
draws a row per *entity* and a red-exclamation row for any entry with none —
and so force a placeholder entity just to carry the row, which then takes the
row's name and icon and brings a service device with it. As a normal
integration none of that applies: the page lists the entries, and each row
opens its configuration.

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
best end-to-end check, and it catches both over- and under-attribution. It
is a property of compiled hours; a hole (below) has no rows to sum.

**Values are cumulative monotonic sums.** Charts use `stat_types: change` to
derive per-bucket values. A sum that decreases is always a bug.

**Rows are dense over every *known* statistic, not merely the states seen in
the window.** `payload.build_payloads` takes `existing`, which `compiler`
sources from the recorder's own `statistics_meta`. Emitting only the window's
own states leaves a statistic with no row in the hour before the next window.
`_async_newest_sum_before` finds its base further back, but that lookup is
one or two extra queries per statistic per compile, meant for the rare hole,
not for every quiet state on every run; and a sparse series still breaks the
`mean`.

A hole is not sparseness: no statistic has a row there, so every sum carries
across it and every average skips the hour alike. Density is a property of
the hours that are compiled.

Density is what makes the `mean` correct. Every row carries the hour's
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

**The state a window opens in has four sources, tried in order.** Each
answers a case the one before it cannot.

1. **The recorder, reading an hour further back than the window.**
   `include_start_time_state` returns exactly *one* row before the boundary,
   so a single ignored row there hides a good state moments behind it.
   Reading the previous hour whole surfaces both, and `canonicalise` folds
   everything before `window_start` into the carried state.
2. **The state machine**, when `last_changed <= window_start` proves the
   live state was already in effect. Proof rather than inference, which is
   why it outranks the two carries below: when it disagrees with our own
   rows, the rows are stale — a change committed late in the hour before
   the window and purged before that hour was recompiled. It is also the
   only source left for an entity purge has erased entirely.
3. **The previous chunk.** A chunk hands the state it ended in to the next,
   so only the opening chunk queries at all. Threaded, never re-read:
   mid-compile the previous chunk's rows are still queued, exactly as with
   `base_sums`.
4. **That hour's own statistics** (opening chunk only). A whole hour with
   nothing recordable in it means the entity held one state throughout — and
   our rows already encode the carry-forward decision, so they *are* the
   resolved timeline. Free: `_async_base` fetches the values in the
   same query as the sums. Only when one duration statistic accounts for the
   hour; several would mean transitions inside it, which the recorder holds.

Then nothing: the window opens later, at the first whole hour that begins
in a recordable transition, and the hours passed over are not compiled at
all (see below). A widening lookback — 1 hour, 1 day, 30 days — would guess
at a distance and give up past a month, where step 4 is exact and has no
distance limit at all.

Step 4 has only the state *token* to hand, since that is all an ID carries.
`_readable_state` recovers the state from the name the statistic already
holds — the half `rename` leaves alone — so a window carried out of
statistics still reads `heat_cool` rather than `heatcool`. Verified, not
trusted: the recovered text must tokenise back to the same token, or the
name did not have the shape assumed and the token stands. Trusting it would
be worse than the token, because a wrong state builds a different ID and
splits the series.

**A compile never opens before the recorder's evidence.** An explicit
`start` earlier than the first whole hour of retained history is raised to
it (`_async_opening_floor`). The hours before have no rows to rebuild them
from, so compiling them does not describe the entity's past, it replaces
it: when our own last row vouches for a state, every span falls to that one
state and every real sum flattens to its base — a deletion by another name.
Those hours stay as they were compiled when the rows still existed. The one
exception is a hole: downtime longer than the horizon leaves hours after the
watermark that were never compiled, so the floor is the hour after the
watermark or the evidence, whichever comes first. Opening there puts the
watermark hour itself in the position the carry chain's fourth source reads,
so a hole our own last row can vouch for is filled with that state, and one
it cannot — the watermark hour was not uniform, and the order of its states
is lost — is left open. That clause is also what keeps the hourly run
working after a purge — without it the trailing window would floor to the
evidence and compile nothing.

**A long compile is chunked, and `_ChunkState` is threaded through it.**
`async_compile` walks the window in `CHUNK_HOURS` slices to bound memory during
a backfill, and each slice returns the sums, the known statistics and the
carried state that the next one starts from. Re-reading any of the three per
chunk would break monotonicity, density or the carry in exactly the way those
invariants describe, because the recorder writes are still queued. They
travel as one named value so the early return — a chunk with nothing to
compile — cannot pass them on in the wrong order.

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

One boundary row is not an event: a row at `window_start` *into the state
already carried*. The state machine's `last_changed` is that row, or an
ignored row sat between two spells of the same state, so counting it would
count a change from a state to itself. `_open_window` drops it; counting it
gives an entity born exactly on the hour a transition on every
trailing-window recompile of its birth hour.

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
`payload.rename`, splitting on the *last* `": "` — which is unambiguous
only because `compose_name` strips colons from the state half. A display
name may hold any number of them; a state may hold none. The state half cannot be
rebuilt from the ID — the ID holds only the token — so a rename would
otherwise never reach a state the entity has not been in for months, and
neither would a change to `mean_type` or the units.

**A state older than the purge horizon is still known.** Purge deletes every
row past `purge_keep_days` with no per-entity reprieve (`queries.py:281`), so
an entity that sits in one state longer than that vanishes from history —
and the quieter the entity, the likelier it is, which is the wrong way round
for this integration. `_carried_from_state_machine` asks the state machine,
which still holds both the state and when it last changed.
`last_changed <= window_start` is what makes it sound rather than a guess: it
proves the state was already in effect when the window opened.
`_async_earliest_state_ts` opens such an entity's history at the first *whole*
hour after `last_changed` for exactly that reason — the hour containing the
change starts before it, so the carried state would be refused and the entity
would compile nothing at all. Refusing it
otherwise is equally load-bearing — without that test a backfill of old hours
would be handed whatever the entity happens to be doing today.

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
`record_known` ignores it. An explicit `states` entry for the raw value wins over
the substitution — blank is the most meaningful state a text error sensor has,
and only the config knows that. The recorder stores NULL when an entity is removed
or reloaded, and the history API hands that back as `""` — which tokenises to
nothing and would leave a double underscore. It is a state we cannot name,
not an absence of data. Letting it reach
`build` instead raises `InvalidStatisticIdError`, and that aborted the whole
entity's compile — permanently, because the watermark never advanced past the
chunk containing it.

**A window no source can open is moved, not filled.** `_open_window`
advances `window_start` to the first whole hour that begins in a recordable
transition, and the hours passed over are not written. They are either
already compiled — a recompute that reached back to an hour the recorder
can no longer open, which keeps the rows written when it could — or a
genuine hole, downtime longer than the horizon that no row vouches for; and
a hole is what a chart should show there, not a band recording our own
ignorance. The base is read from the newest row *before* the window, not
strictly the hour before it (`_async_base`), so a sum carries across a hole
unchanged: time we cannot describe is time in no state, and `change` over
the hole is zero. `last_reset` is no help here —
`_augment_result_with_change` (`recorder/statistics.py:2036`) computes
`change` as `sum - prev_sum` and never reads it; a series restarted at zero
would chart the drop.

After a move the base is read again for the new start, which is safe only
because a window moves only when no state was carried into it, and once a
chunk has written anything the next one is handed the state it ended in —
so nothing is queued yet. Do not re-read it anywhere else.

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

Verified against 2026.8.3.

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
  sum is known, which is why the base is read from the newest bucket *before*
  the window rather than the newest one overall — a recompute opening inside a
  hole has rows on both sides, and the ones ahead are what it overwrites.
- `Compiler.async_compile` drains the recorder in a `finally`, not on the
  success path. A chunk that raises leaves earlier chunks' writes queued, and
  density is read live from `statistics_meta` — so the next compile could
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

Every test must fail when its fix is reverted; a test that passes regardless
of the code under test proves nothing. When adding a test for a bug fix,
revert the fix, watch it fail, then restore.

Integration tests use `recorder_mock` and `freezer`. Each integration test
module overrides the root conftest's autouse `auto_enable_custom_integrations`
fixture to request `recorder_db_url` first; this is fixture ordering against
`recorder_mock`, not a workaround.
