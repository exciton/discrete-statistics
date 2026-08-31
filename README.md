# Discrete Statistics for Home Assistant

Long-term statistics for binary and enum entities: how many times an entity
entered each state, and how long it spent there. Retained forever,
independent of `purge_keep_days`.

Home Assistant's own long-term statistics cover numeric sensors only, so
the history of a binary sensor disappears when the recorder purges. This
component derives per-state counters from recorder history and writes them
as external statistics, which are never purged.

## Installation

Copy `custom_components/discrete_stats` into your `config/custom_components`
directory and restart Home Assistant. Requires Home Assistant 2026.8.3 or
later.

## Configuration

```yaml
discrete_stats:
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
| `name` | the entity's ID | used in statistic display names |
| `default` | `record_known` | disposition for states not listed |
| `states` | `{}` | per-state overrides |

`default` accepts:

- `record` — every state, including `unavailable` and `unknown`
- `record_known` — every real state; `unavailable`/`unknown` carry forward
- `ignore` — only states listed in `states:` are recorded

Each entry in `states:` is one of:

- `ignore` — carry the previous state forward
- `record` — record it, overriding `default`
- another state name — map onto that state

```yaml
discrete_stats:
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

## Statistics produced

For each state, two statistics:

```
discrete_stats:<entity>_<state>_seconds
discrete_stats:<entity>_<state>_count
```

Both are cumulative sums, so charts use the `change` stat type to show
per-bucket values. Statistics for a state appear the first time that state
is observed — no configuration change is needed when a new state shows up.

### The `no_data` state

When the component cannot attribute a span to a real state — before an
entity's first known state, or across a gap whose source rows have been
purged — it attributes the time to `no_data`. Durations therefore always
sum to 24h per day, and gaps are visible in the chart rather than silently
filled in.

`no_data` has a `_seconds` statistic only. Nothing transitions *into* it —
it is what is left when no state can be carried in — so there is no
`no_data_count`.

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
  - discrete_stats:binary_sensor_grid_status_off_count
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
  - discrete_stats:binary_sensor_grid_status_on_seconds
  - discrete_stats:binary_sensor_grid_status_off_seconds
  - discrete_stats:binary_sensor_grid_status_no_data_seconds
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
  - discrete_stats:sensor_heat_pump_hvac_action_heating_seconds
  - discrete_stats:sensor_heat_pump_hvac_action_cooling_seconds
  - discrete_stats:sensor_heat_pump_hvac_action_idle_seconds
```

A state that appears later accumulates immediately but must be added to the
card's `entities` list to be drawn.

## Backfilling

Statistics are compiled forward from the moment the component is installed.
To derive them from history the recorder still holds:

```yaml
action: discrete_stats.recompute
data:
  entity_id: binary_sensor.grid_status
```

Omitting `start` backfills from the oldest retained state. Once that
completes, `purge_keep_days` can be reduced without losing the derived
statistics.

To repair a range after correcting history:

```yaml
action: discrete_stats.recompute
data:
  entity_id: binary_sensor.grid_status
  start: "2026-01-01T00:00:00Z"
```

`clear: true` deletes the entity's existing statistics first, including
their metadata. Use it only to remove statistics that should no longer
exist, such as an orphaned state after a mapping change. It cannot be
combined with `start` — clearing removes every hour, not only the ones
after `start`, so it always rebuilds from the oldest retained state.
Passing both is rejected.

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
