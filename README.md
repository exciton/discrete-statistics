# Discrete Statistics for Home Assistant

Long-term statistics for binary and enum entities: how many times an entity
entered each state, and how long it spent there. Retained forever,
independent of `purge_keep_days`.

Home Assistant's own long-term statistics cover numeric sensors only, so
the history of a binary sensor disappears when the recorder purges. This
component derives per-state counters from recorder history and writes them
as external statistics, which are never purged.

![A year of a heat pump's mode, week by week: hours in heat, cool and off](https://raw.githubusercontent.com/exciton/discrete-statistics/main/docs/images/heat-pump-weekly.png)

- Works with any entity whose state is a label: binary sensors, covers,
  climate, `hvac_action`, enum sensors, `input_select`, `person`…
- Records hourly long-term statistics per state: time spent in it, and the
  number of times it was entered
- Stored as external statistics, so they are never purged — kept forever,
  independent of `purge_keep_days`
- Backfills from the recorder's existing history on first run, so a new
  entity starts with whatever the recorder still holds rather than from zero
- Draws with the stock statistics-graph card: `change` for totals over
  days, weeks and months; `mean`, `min` and `max` for average and peak hours
- The `mean` of a duration over any period is the share of that period
  spent in the state — `0.4` is 40 % — straight from the card
- New states are picked up automatically as they appear; no per-state
  configuration needed
- `unavailable`, `unknown`, or any state you choose can be ignored, with
  the previous state carried across the gap instead of a hole
- Debounce: a state that lasts less than a minimum duration can be ignored,
  per state or for every state
- States can be mapped onto one another (`heat_cool` → `heating`)
- Set up from the UI or YAML; per-state mappings are YAML-only for now
- Recalculate any range at any time — it only rewrites what it has source
  data for, and never deletes anything

It is not a replacement for `history_stats`, which answers a different
question; [the comparison below](#compared-with-history_stats) says which to
reach for.

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
| `min_duration` | — | how long a spell of a conditionally recorded state must last |

`default`, `blank` and `min_duration` are available in the UI as well;
per-state mappings are still YAML-only.

`default` accepts:

- `record` — every state, including `unavailable` and `unknown`
- `record_known` — every real state; `unavailable`/`unknown` carry forward
- `ignore` — only states listed in `states:` are recorded
- `ignore_short` — every state, but a spell shorter than `min_duration`
  carries the previous state forward
- `ignore_short_unknown` — every real state; `unavailable`/`unknown` only
  when the spell lasts `min_duration`

Each entry in `states:` is one of:

- `ignore` — carry the previous state forward
- `record` — record it, overriding `default`
- `ignore_short` — record it, unless the spell is shorter than `min_duration`
- another state name — map onto that state

Some states have no name to record under: empty, blank or made only
of whitespace and punctuation. `blank:` says what becomes of those. It takes
either:

- `ignore` — carry the previous state forward
- a state name — substitute it

The substitute is resolved like any other state, so with the default
`blank: unknown` a blank state is treated as if the entity had reported
`unknown` — ignored by `record_known`, recorded by `record`. `blank: ignore`
is narrower: it ignores blanks only, and leaves real `unknown` states alone.

A blank state often carries real meaning. A text sensor that reports `""`
for "no error" wants that recorded as a state of its own, and `states:` is
where to say so:

```yaml
discrete_statistics:
  - entity_id: sensor.pump_error
    states:
      "": ok
      unavailable: offline
    blank: ignore
```

`states:` is consulted first, so `""` maps to `ok` before `blank:` is ever
looked at. `blank:` then applies to whatever blank states are *not* listed —
here, anything made of whitespace or punctuation is carried forward. Without
the mapping, `""` would be substituted with `unknown` and — under the default
`record_known` — carried forward, crediting the time to whichever error was
last seen.

### Short spells

`ignore_short` records a state only when the entity stays in it for at least
`min_duration`, whether as a `states:` entry or as the `default`. A shorter spell is carried across as though the entity had
never left the state before it: no transition is counted, and the time goes
to the state it interrupted. `min_duration` takes a duration — `00:00:30`,
`{minutes: 5}` — and can be at most one hour.

Two uses. A device that drops off the network for a few seconds on every
router reboot, but whose real outages are worth a band on the chart:

```yaml
discrete_statistics:
  - entity_id: binary_sensor.grid_status
    default: ignore_short_unknown
    min_duration: "00:05:00"
```

`on` and `off` are recorded as they come. A five-minute outage is recorded
as five minutes of `unavailable` and one transition; a twenty-second blip is
twenty more seconds of `on`, and no transition at all. The same for one
state only is a `states:` entry — `unavailable: ignore_short` with the
`default` left alone.

And a contact that bounces — a door that reads `off`, `on`, `off` in the
half-second it takes to close:

```yaml
discrete_statistics:
  - entity_id: binary_sensor.garage_door
    default: ignore_short
    min_duration:
      seconds: 5
```

Every state is conditional then, so the bounce is not counted and the door
closed once. `unavailable` and `unknown` are recorded under it whenever
they last long enough; add `unavailable: ignore` to `states:` to carry them
forward regardless.

Each spell is judged on its own length, not on the run it sits in: `on` for
two seconds then `unknown` for two seconds, under a five-second threshold, is
two short spells, not one four-second one. Until a spell has ended the
component cannot know how long it will be, so an hour compiled while one is
running treats it as short and is compiled again once the answer is in —
the same trailing recompile that picks up a late-committed state.

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

## Configuring from the UI

Settings → Devices & Services → **Add integration** → **Discrete
Statistics**. Pick an entity, optionally name it, and choose which states
to record; the two choices that mention a minimum duration read the
duration field below them. Compiling starts in the
background as soon as you press Submit,
and a notification reports how many hours were compiled: the entity's full
retained history for a genuinely new entity, or just the trailing window if
it was previously configured and deleted, since statistics are kept on
removal and compiling resumes from that watermark.

![The options dialog: name, the states-to-record dropdown open on its four choices, blank states, and minimum duration](https://raw.githubusercontent.com/exciton/discrete-statistics/main/docs/images/options-dialog.png)

Changing an entry's recording rule or minimum duration recompiles that
entity's whole history, so the change applies to the past as well as the
future; changing only its name does not.

The entity itself cannot be changed after creation: it determines the
statistic IDs, so a change would orphan the existing series. Delete the
entry and make a new one instead.

An entity may be configured once, either in YAML or through the UI. The
dialog refuses an entity that YAML already configures; a YAML block added
later for an entity the UI owns disables that entry and raises a repair
issue.

Removing an entry stops compiling. It never deletes statistics — do that
in Settings → System → Tools → Statistics.

Entities that report a *measurement* are refused — anything with a
`state_class` or a unit. Each distinct reading would otherwise become its own
pair of statistics, written every hour forever. The check is on submit rather
than in the picker, because "has no unit" cannot be expressed as a picker
filter, and a domain allowlist would exclude enum `sensor.*` entities, which
are a main use case.

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

### Blank states

A state that cannot be recorded at all is treated as `unknown` rather than as
a gap: an empty one, which Home Assistant produces when an entity is removed
or reloaded, or one made only of punctuation. So it is ignored or recorded
according to the same setting that governs `unknown`.

### Gaps

An hour the component cannot open in a known state is not recorded. That
is the hour before an entity's first state — it rarely lands exactly on
the hour, and recording the minutes before it would describe nothing more
than the moment it was switched on — and, rarely, a stretch after the
integration has been off for longer than the recorder's `purge_keep_days`,
when the source rows for it are gone and nothing else can vouch for the
state. Such hours have no rows at all: a chart shows nothing there, an
average skips them, and the cumulative totals carry across unchanged.
Everything on either side is untouched, and a `recompute` reaching into
the stretch leaves it alone too.

## Charts

Time in each state per day, stacked:

```yaml
type: statistics-graph
title: Grid Status
chart_type: bar-stack
period: day
days_to_show: 30
stat_types:
  - change
entities:
  - discrete_statistics:binary_sensor_grid_status_on_duration
  - discrete_statistics:binary_sensor_grid_status_off_duration
```

![Thirty days of grid status: a full bar of on each day, with two short bands of off](https://raw.githubusercontent.com/exciton/discrete-statistics/main/docs/images/grid-state-daily.png)

Outages per month:

```yaml
type: statistics-graph
title: Monthly Outages
chart_type: bar
period: month
days_to_show: 365
stat_types:
  - change
entities:
  - discrete_statistics:binary_sensor_grid_status_off_count
```

![A year of outages per month, none to five](https://raw.githubusercontent.com/exciton/discrete-statistics/main/docs/images/outages-monthly.png)

A year of a heat pump's mode, week by week — the chart at the top of this
page:

```yaml
type: statistics-graph
title: Heat Pump
chart_type: bar-stack
period: week
days_to_show: 365
stat_types:
  - change
entities:
  - discrete_statistics:climate_heat_pump_heat_duration
  - discrete_statistics:climate_heat_pump_cool_duration
  - discrete_statistics:climate_heat_pump_off_duration
```

How often a light is switched on in an average hour each week, and in the
busiest hour:

```yaml
type: statistics-graph
title: Mean/Max Hourly Light On
chart_type: line
period: week
days_to_show: 365
stat_types:
  - mean
  - max
entities:
  - discrete_statistics:light_kitchen_lights_on_count
```

![A year of kitchen light switch-ons: the mean hovers near 0.2 an hour, the busiest hour of each week between one and four](https://raw.githubusercontent.com/exciton/discrete-statistics/main/docs/images/light-count-mean-max.png)

The share of time a light is on, as the `mean` of its duration — hours per
hour is a fraction, so 0.12 is 12 %:

```yaml
type: statistics-graph
title: Average Hourly Lighting
chart_type: line
period: week
days_to_show: 365
stat_types:
  - mean
entities:
  - discrete_statistics:light_kitchen_lights_on_duration
```

![A year of the kitchen light's share of time on, between 3 % and 19 % week by week](https://raw.githubusercontent.com/exciton/discrete-statistics/main/docs/images/light-share-of-time.png)

A state that appears later accumulates immediately but must be added to the
card's `entities` list to be drawn.

## Backfilling

An entity that has not changed within the recorder's window has no history at
all — purge keeps nothing per entity — but it is still recorded: Home
Assistant knows its current state and when that began, which is enough to
account for every whole hour since. An entity with neither history nor a
current state records nothing.

A newly configured entity compiles its whole retained history on its first
ordinary run — there is no watermark to trail, so there is nothing to do but
start at the beginning. `recompute` is for the cases that first run cannot
cover: re-attributing history after a configuration change, or repairing a
range.

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

## Compared with `history_stats`

Home Assistant's own [`history_stats`](https://www.home-assistant.io/integrations/history_stats/)
answers a different question. It is a sensor whose value is *how much of a
window* an entity spent in some states — the window being whatever its
`start`/`end` templates render to right now — and it reads that from the
recorder each time. This component writes the answer for every hour, once,
into statistics that outlive the recorder. Each is the right tool for a specific job.

### The same chart, both ways

Grid outages per day and hours off-grid per day, for the last year. With
`history_stats`, the statistics have to come from the recorder's own
handling of the sensors, so each one needs a window that resets at midnight
and a `state_class` the recorder will sum:

```yaml
sensor:
  - platform: history_stats
    name: Grid off today
    unique_id: grid_off_today
    entity_id: binary_sensor.grid_status
    state: "off"
    type: time
    start: "{{ today_at('00:00') }}"
    end: "{{ now() }}"
    state_class: total_increasing
  - platform: history_stats
    name: Grid outages today
    unique_id: grid_outages_today
    entity_id: binary_sensor.grid_status
    state: "off"
    type: count
    start: "{{ today_at('00:00') }}"
    end: "{{ now() }}"
    state_class: total_increasing
```

```yaml
type: statistics-graph
period: day
days_to_show: 365
stat_types:
  - change
entities:
  - sensor.grid_off_today
  - sensor.grid_outages_today
```

With this component:

```yaml
discrete_statistics:
  - entity_id: binary_sensor.grid_status
```

```yaml
type: statistics-graph
period: day
days_to_show: 365
stat_types:
  - change
entities:
  - discrete_statistics:binary_sensor_grid_status_off_duration
  - discrete_statistics:binary_sensor_grid_status_off_count
```

The two cards look alike. The first one is wrong in ways that are hard to
see:

- **It starts today.** The sensors have no value before they exist, so the
  chart is empty for the past year and fills in from now. The second reaches
  back as far as the recorder held history when the entity was first
  compiled.
- **Midnight is detected, not known.** `total_increasing` has no reset
  signal; the recorder infers one when the value drops below 90 % of the
  previous reading. Whether an outage that spans midnight is counted once
  or twice therefore depends on the day before: after a day with one outage
  the count reads `1` on both sides of midnight, no drop, no reset, counted
  once; after a day with two it drops from `2` to `1`, a reset, and the
  outage is counted again. The second card credits it to the hour it began.
- **A restart during the outage splits it.** `unavailable` is not `off`, so
  the interval closes and a new one opens, the count goes up, and the
  downtime is attributed to nothing. The second card carries `off` across
  it.
- **Three more states means six more sensors**, each with the same window
  templates to keep right, and a state the entity has not shown yet has no
  sensor at all.
- **The value is a sensor's state**, so it is recorded to the recorder like
  any other, purged like any other, and the statistics are derived from
  samples of it rather than from the transitions themselves.

None of that is a defect in `history_stats`: it was built to show a live
figure, and the long-term statistics are a by-product of giving that figure
a `state_class`. Writing the statistics directly is the point of this
component.

### Things this component does that `history_stats` cannot

**Every state of an enum, from one line.** A heat pump's `hvac_action` has
`heating`, `cooling`, `idle`, `defrosting` and whatever next year's firmware
adds. One entry here records all of them, duration and count, and a state
that appears later gets its statistics the first hour it is seen.
`history_stats` matches one set of states per sensor and merges the set into
one figure, so *time in each of N states* is N sensors, counts are N more,
and a new state is a new sensor you have to know to create.

**Survives `purge_keep_days`.** Statistics are never purged. A
`history_stats` window that reaches past the recorder's retention returns
a smaller number, silently: `0` hours over a range that was never recorded
looks exactly like `0` hours in the state. Once this component has compiled
the history, retention can be shortened without losing the series.

**Hours that sum to the day.** Every state's duration is written for every
hour, so a stacked bar of all of an entity's states is 24 h tall, and
`mean` and `max` over a day are the average and the busiest hour — with quiet
hours counted as quiet, not skipped.

### Things `history_stats` does that this component cannot

**A number right now.** Time in state *so far today*, updated on every change
and at least once a minute. This component writes an hour after it closes;
nothing here is ever more current than the last whole hour.

**Any window.** The last thirty minutes, since sunrise, until 4 pm, the
previous calendar month: whatever a template can render. This component has
hourly buckets, and only what Home Assistant's statistics cards can do with
them.

**An entity.** A sensor can sit on a card, gate an automation, and be read in
a template. Statistics are only reachable through the statistics cards and
the recorder's websocket API.

Its `ratio` type is not on that list. Hours per hour is already a fraction:
the `mean` of a duration statistic over any period *is* the share of that
period spent in the state - a `mean` of `0.4` is 40 % - and it is what the last
chart under *Charts* draws.

### Side by side

| | `history_stats` | `discrete_statistics` |
|---|---|---|
| Produces | one sensor: a value for the current window | per-state duration and count statistics, per hour |
| Freshness | on change, at least every minute | after each hour closes |
| Resolution | seconds, within the window | hourly buckets |
| Reach into the past | as far as the recorder's retention | whole retained history on first run, kept forever after |
| Backfill | none — begins when the sensor is created | first run, and `recompute` for any range with history |
| After purge | window silently shrinks toward `0` | statistics unaffected |
| States per entity | one set per sensor, merged into one number | every state, automatically |
| A new state | a new sensor, when you notice | recorded from its first hour |
| Count means | intervals in the window; a state active at the start counts | transitions into the state, in the hour they happen |
| `unavailable` / `unknown` | not in the list, so they break the interval | carry the previous state forward; configurable |
| State mapping | none | `states:` map, `default`, `blank` |
| Window | any template; two of `start`/`end`/`duration` | none — hourly, and whatever the cards aggregate |
| Share of time | `ratio` % | `mean` of a duration: hours per hour is a fraction |
| Debounce | `min_state_duration` | `ignore_short` with `min_duration`, per state or as the default |
| Usable in automations | yes, it is a sensor | no |
| Configuration | UI with live preview, or YAML; one sensor per state × metric × window | UI or YAML; one entry per entity |
| Long-term statistics | of the sensor's own value (`measurement`), or `total_increasing` with reset detection | are the product |

Use both: `history_stats` for the tile that says how long the door has been
open today, this component for the chart of how often it was opened each
week this year.

## Limitations

- Hourly buckets only. The external statistics API writes only to the
  hourly table.
- An in-progress state change is not charted until its hour closes.
- A state committed later than the trailing window needs a manual
  `recompute`.
- Charts name their statistics explicitly; a newly appearing state must be
  added to the card.
- Hours the component was not running for, beyond the recorder's
  `purge_keep_days`, are recorded only when its own last row can vouch for
  the state; otherwise they stay empty (see *Gaps*).
