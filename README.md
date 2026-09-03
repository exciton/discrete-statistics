# Discrete Statistics for Home Assistant

Long-term statistics for binary and enum entities: how many times an entity
entered each state, and how long it spent there. Retained forever,
independent of `purge_keep_days`.

Home Assistant's own long-term statistics cover numeric sensors only, so
the history of a binary sensor disappears when the recorder purges. This
component derives per-state counters from recorder history and writes them
as external statistics, which are never purged.

## Installation

Requires Home Assistant 2026.8.3 or later.

### HACS (recommended)

This is not in the HACS default store, so add it as a custom repository:

1. HACS → three-dot menu → **Custom repositories**
2. Repository: `https://github.com/exciton/discrete-statistics`
3. Type: **Integration**
4. Add, then install **Discrete Statistics**
5. Restart Home Assistant

### Manually

Copy `custom_components/discrete_statistics` into your `config/custom_components`
directory and restart Home Assistant.

## Configuration

```yaml
discrete_statistics:
  - entity_id: binary_sensor.grid_status
    name: "Grid Status"
```

`unavailable` and `unknown` are ignored by default: the previous state
carries forward, so a Home Assistant restart does not look like a state
change.

### Options

| key | default | meaning |
|---|---|---|
| `entity_id` | required | the entity to track |
| `name` | the entity's own name | used in statistic display names |
| `default` | `record_known` | disposition for states not listed |
| `states` | `{}` | per-state overrides |
| `blank` | `unknown` | what to do with a state that has no letters or digits |

Both `default` and `blank` are available in the UI as well; per-state
mappings are still YAML-only.

`default` accepts:

- `record` — every state, including `unavailable` and `unknown`
- `record_known` — every real state; `unavailable`/`unknown` carry forward
- `ignore` — only states listed in `states:` are recorded

Each entry in `states:` is one of:

- `ignore` — carry the previous state forward
- `record` — record it, overriding `default`
- another state name — map onto that state

Some states have no name to record under: an empty one, which Home Assistant
produces when an entity is removed or reloaded, and the rarer state made only
of whitespace or punctuation. `blank:` says what becomes of those. It takes
either:

- `ignore` — carry the previous state forward
- a state name — substitute it

A name is substituted *before* `default` is applied, so the stock `unknown`
behaves exactly like a real `unknown` — ignored by `record_known`, recorded by
`record`. `blank: ignore` is different from that: it ignores blanks only, and
leaves genuine `unknown` states alone.

`no_data` is a legal answer for both `blank:` and a `states:` target
— that is how you say "I cannot interpret this state, chart it as a gap". A
device cannot reach that band on its own, though: a sensor that literally
reports the string `no_data` is ignored unless your config names it.

A blank state often carries real meaning, so an entry in `states:` beats the
substitution. A text sensor that reports `""` for "no error" wants:

```yaml
discrete_statistics:
  - entity_id: sensor.pump_error
    blank: ok                # or, equivalently here:
    states:
      "": ok
      unavailable: offline
```

Without one of those, "no error" would be treated as `unknown` and — under
the default `record_known` — carried forward, crediting the time to whichever
error was last seen.

```yaml
discrete_statistics:
  # chart dropouts as their own band
  - entity_id: binary_sensor.grid_status
    states:
      unknown: record
      unavailable: unknown

  # closed vocabulary; a new state cannot appear
  - entity_id: sensor.heat_pump_hvac_action
    default: ignore
    states:
      heating: record
      cooling: record
      idle: record
      cool: cooling
```

`no_data` is reserved and cannot be used as a state name or map target.

## Configuring from the UI

Settings → Devices & Services → Helpers → **Create helper** → **Discrete
Statistics**. Pick an entity, optionally name it, and choose which states
to record. Compiling starts in the background as soon as you press Submit,
and a notification reports how many hours were compiled: the entity's full
retained history for a genuinely new entity, or just the trailing window if
it was previously configured and deleted, since statistics are kept on
removal and compiling resumes from that watermark.

Changing a helper's recording rule recompiles that entity's whole history, so
the change applies to the past as well as the future; changing only its name
does not.

The entity itself cannot be changed after creation: it determines the
statistic IDs, so a change would orphan the existing series. Delete the
helper and make a new one instead.

An entity may be configured once, either in YAML or as a helper. The
helper dialog refuses an entity that YAML already configures; a YAML block
added later for an entity that a helper owns disables that helper and
raises a repair issue.

Removing a helper stops compiling. It never deletes statistics — do that
in Settings → System → Tools → Statistics.

YAML configuration keeps working unchanged, and is still the only way to
set per-state dispositions and the `ignore` default.

## Statistics produced

For each state, two statistics:

```
discrete_statistics:<entity>_<state>_duration
discrete_statistics:<entity>_<state>_count
```

Separators are stripped from the state, so it is always one word in the ID:
`heat_cool` becomes `..._heatcool_duration`. The readable state stays in the
statistic's name. Two states that differ only by separators therefore share a
statistic and are recorded together.

Statistics are named `<entity>: <state> (h)` for durations and `(#)` for
counts, with the state rendered the way Home Assistant renders it — a door
sensor reads `Open`/`Closed`, not `on`/`off`.

*Limitation:* that rendering uses the **instance** language, from Settings →
System → General, because the name is one stored string with no viewer in
scope. Home Assistant's own screens translate per user, so a household whose
members use different languages sees one language here.

Both are cumulative sums, so charts use the `change` stat type to show
per-bucket values. Durations are in **hours**, so an hourly bucket in a single
state reads as `1.0` and a full day sums to `24`. Statistics for a state appear the first time that state
is observed — no configuration change is needed when a new state shows up.

Each statistic also carries the hour's own value as its `mean`, `min` and
`max`. Over a longer period Home Assistant reduces the hours itself, so those
stat types answer questions the cumulative sum cannot: `mean` over a day is
the **average hourly** duration or count, `max` is the busiest hour and `min`
the quietest. Every hour has a row — including the ones in which nothing
happened — so the average is over the whole period rather than only its
active hours.

### The `no_data` state

When the component cannot attribute a span to a real state — before an
entity's first known state, or across a gap whose source rows have been
purged — it attributes the time to `no_data`. Durations therefore always
sum to 24h per day, and gaps are visible in the chart rather than silently
filled in.

`no_data` has a `_duration` statistic only, whether the compiler chose it or
your config did: it measures time nobody can account for, and counting those
spans would tell you nothing.

A state that cannot be recorded at all is treated as `unknown` rather than as
a gap: an empty one, which Home Assistant produces when an entity is removed
or reloaded, or one made only of punctuation. So it is ignored or recorded
according to the same setting that governs `unknown`.

A statistic is never *opened* with `no_data`, though. An entity's first state
rarely lands exactly on the hour, and recording the few minutes before it would
give every entity a permanent `no_data` statistic describing nothing more than
the moment it was switched on. Compilation starts at the first whole hour whose
state is known, so most entities never grow a `no_data` statistic at all — and
one that appears later is a genuine gap worth looking at.

## Charts

Outages per day for the last week:

```yaml
type: statistics-graph
title: Grid outages per day
chart_type: bar
period: day
days_to_show: 7
stat_types:
  - change
entities:
  - discrete_statistics:binary_sensor_grid_status_off_count
```

Time in each state per day, stacked:

```yaml
type: statistics-graph
title: Grid state
chart_type: bar-stack
period: day
days_to_show: 30
stat_types:
  - change
entities:
  - discrete_statistics:binary_sensor_grid_status_on_duration
  - discrete_statistics:binary_sensor_grid_status_off_duration
  - discrete_statistics:binary_sensor_grid_status_nodata_duration
```

Weekly heat pump behaviour over three months:

```yaml
type: statistics-graph
title: Heat pump
chart_type: bar-stack
period: week
days_to_show: 90
stat_types:
  - change
entities:
  - discrete_statistics:sensor_heat_pump_hvac_action_heating_duration
  - discrete_statistics:sensor_heat_pump_hvac_action_cooling_duration
  - discrete_statistics:sensor_heat_pump_hvac_action_idle_duration
```

Average hourly run time per day, and the busiest hour of each day:

```yaml
type: statistics-graph
title: Heat pump hourly run time
chart_type: line
period: day
days_to_show: 30
stat_types:
  - mean
  - max
entities:
  - discrete_statistics:sensor_heat_pump_hvac_action_heating_duration
```

A state that appears later accumulates immediately but must be added to the
card's `entities` list to be drawn.

## Backfilling

A new helper compiles the entity's whole retained history on its first
ordinary run — there is no watermark to trail, so there is nothing to do but
start at the beginning. `recompute` is for the cases that first run cannot
cover: re-attributing history after a configuration change, repairing a range,
or filling in hours written before a feature existed.

```yaml
action: discrete_statistics.recompute
data:
  entity_id: binary_sensor.grid_status
```

Omitting `start` backfills from the oldest retained state. Once that
completes, `purge_keep_days` can be reduced without losing the derived
statistics.

To repair a range after correcting history:

```yaml
action: discrete_statistics.recompute
data:
  entity_id: binary_sensor.grid_status
  start: "2026-01-01T00:00:00Z"
```

Hours compiled before `mean`, `min` and `max` were added carry only a sum.
Home Assistant leaves a missing mean out of its rollup rather than counting it
as zero, so a day made partly of such hours would report a misleadingly high
average. A recompute over the range fills them in.

### Recompute never deletes

`recompute` only writes. It rewrites the buckets it has recorder history for
and leaves everything outside that range untouched, so a rebuild can never
discard statistics whose source states have already been purged.

A consequence worth knowing: if you change a state mapping, the statistics for
the old state stop growing but remain as a historical record. That is
deliberate — they describe hours that really happened, and the recorder can no
longer prove otherwise. Drop them from your charts if they are noise.

To delete one properly, use Home Assistant's own tool at **Settings → System →
Tools → Statistics**, which removes a single statistic with a confirmation
step. It stays deleted — nothing else records that it existed, so the next
compile simply stops writing it. The one exception is a state that happens
again: an observed state is always recorded, both its duration and its count. Deletion should be a decision you make, not a side effect of a routine
rebuild.

## How it works

Every hour at `:03`, the component reads recorder history for each
configured entity, resolves raw states through the disposition table,
splits durations at hour boundaries, and writes cumulative sums.

Each run recomputes the trailing three hours. The recorder is a queue, so a
state change at `10:59:58` may not be committed when the hour is first
compiled; recomputing picks it up, and the recorder's upsert on
`(metadata_id, start_ts)` makes the correction invisible. The same property
means the component can run at any cadence, catch up after downtime, and
backfill using one code path.

Runs are skipped while the recorder's queue is deep, since compiling is
idempotent and the next run catches up.

## Limitations

- Hourly buckets only. The external statistics API writes only to the
  hourly table.
- An in-progress state change is not charted until its hour closes.
- A state committed later than the trailing window needs a manual
  `recompute`.
- Charts name their statistics explicitly; a newly appearing state must be
  added to the card.
- An entity that stays `unavailable`/`unknown` for more than 30 days may have
  part of that stretch attributed to `no_data` rather than to its last known
  state, depending on when compilation runs. Durations still sum correctly.
