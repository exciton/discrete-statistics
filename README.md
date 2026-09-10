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
- Ships its own card: pick the entity and it draws every state, as
  stacked or plain bars or lines per hour, day, week, month or year, in
  hours, days or as a percentage of the time — following the dashboard's
  date picker if there is one
- Draws with the stock statistics-graph card too: `change` for totals over
  days, weeks and months; `mean`, `min` and `max` for average and peak hours
- Period sensors, opt-in: "time on today", "share of this month at the
  office", "openings this year" — a number read from the statistics, so it
  never shrinks when the recorder purges, following the entity live
- The `mean` of a duration over any period is the share of that period
  spent in the state — `0.4` is 40 % — straight from the stock card
- New states are picked up automatically as they appear, in the card as
  well as the statistics; no per-state configuration needed
- `unavailable`, `unknown`, or any state you choose can be ignored, with
  the previous state carried across the gap instead of a hole
- Debounce: a state that lasts less than a minimum duration can be ignored,
  per state or for every state
- States can be mapped onto one another (`heat_cool` → `heating`)
- Set up from the UI or YAML
- Recalculate any range at any time — it only rewrites what it has source
  data for, and never deletes anything

It is not a replacement for `history_stats`, which answers a different
question; [the comparison below](#compared-with-history_stats) says which to
reach for.

## Installation

Requires Home Assistant 2026.8.3 or later.

### HACS (recommended)

This is not in the HACS default store, so add it as a custom repository:

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=exciton&repository=discrete-statistics&category=integration)

Or by hand:

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

All four are available in the UI as well; `states` is the options dialog's
**States** section, and the `ignore` default is YAML-only.

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

[![Add Discrete Statistics](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=discrete_statistics)

Or Settings → Devices & Services → **Add integration** → **Discrete
Statistics**. Pick an entity, then fill in the dialog shown below: a name
if the entity's own will not do, and which states to record - the two
choices that mention a minimum duration read the duration field below
them, a minute if it is left blank. The same dialog is the entry's options dialog afterwards, so nothing
is set on creation that cannot be changed later. Compiling starts in the
background as soon as the entry is created,
and a notification reports how many hours were compiled: the entity's full
retained history for a genuinely new entity, or just the trailing window if
it was previously configured and deleted, since statistics are kept on
removal and compiling resumes from that watermark.

![The options dialog: name, the states-to-record dropdown open on its four choices, minimum duration, and the States section with a row per state and one for blank states](https://raw.githubusercontent.com/exciton/discrete-statistics/main/docs/images/options-dialog.png)

The dialog has a **States** section with a row for every state the
entity has reported — its history, its current state and, for an enum
sensor, its `options` — then `unavailable` and `unknown`, which every
entity can report, folded away until something in it is set. Each row
is the `states:` entry for that state: leave it following the recording
rule above, record it, record it only when it lasts the minimum duration,
ignore it, or pick another state to record it as. Typing a name the entity
has never reported works too. A state that appears later follows the
recording rule, as in YAML. The section's last row is `blank:`, for a state
with no letters or digits, and the section is open when either is set.

Changing an entry's recording rule, a state's row or the minimum duration
recompiles that entity's whole history, so the change applies to the past
as well as the future; changing only its name does not.

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
set the `ignore` default.

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

The statistics are ordinary long-term statistics, so the stock
statistics-graph card draws them; the integration also ships its own card
(below), which is configured by entity rather than by statistic ID and
draws every state the entity has, including one it gains later. Each
example here is given both ways.

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

The same chart from the card:

```yaml
type: custom:discrete-statistics-card
title: Grid Status
entity: binary_sensor.grid_status
period: day
days_to_show: 30
```

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

```yaml
type: custom:discrete-statistics-card
title: Monthly Outages
entity: binary_sensor.grid_status
metric: count
states:
  - "off"
chart_type: bar
period: month
days_to_show: 365
```

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

```yaml
type: custom:discrete-statistics-card
title: Heat Pump
entity: climate.heat_pump
period: week
days_to_show: 365
```

How often a light is switched on in an average hour each week, and in the
busiest hour — a chart only the stock card draws, since the card below
has no `min` or `max`:

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

The card draws the same share as a percentage, on an axis that fits it:

```yaml
type: custom:discrete-statistics-card
title: Kitchen Lights
entity: light.kitchen_lights
unit: percent
chart_type: line
period: week
days_to_show: 365
states:
  - "on"
hide_legend: true
```

![A year of the kitchen light's share of time on, between 2 % and 19 % on an axis topping out at 20 %](https://raw.githubusercontent.com/exciton/discrete-statistics/main/docs/images/lights-share-of-time-custom.png)

With the stock card a state that appears later accumulates immediately
but must be added to the card's `entities` list to be drawn; the card
below draws it as soon as it has statistics.

## The card

The integration ships its own card, so nothing needs adding under
Resources. It draws one entity's states, stacked bars by default, and is
configured by entity rather than by statistic ID:

```yaml
type: custom:discrete-statistics-card
entity: climate.living_room
title: Heat pump
metric: duration        # duration (time in state) or count (transitions)
unit: percent           # auto, h, d or percent; only for duration
period: month           # auto, hour, day, week, month or year
chart_type: bar-stack   # bar-stack, bar, line-stack or line
days_to_show: 365
```

![The card's editor: an entity picker, chart type and period radio buttons, days to show, and the metric and unit dropdowns, beside a year of a heat pump's modes as stacked percent bars](https://raw.githubusercontent.com/exciton/discrete-statistics/main/docs/images/card-config.png)

Every state the entity has statistics for is drawn, in the names the
statistics carry. `states:` narrows and orders them; `ignore_states:`
drops some and keeps the rest:

```yaml
states:          # only these, in this order
  - heat
  - cool
```

```yaml
ignore_states:   # everything but these, alphabetically
  - unavailable
```

Together they order the front and leave the list open: the states in
`states:` come first, then every other state alphabetically, so a state
the entity gains later still shows up, at the end:

```yaml
states:
  - heat
  - cool
ignore_states:
  - unavailable
```

A `states:` entry can carry a name to draw the state under and a colour
— a theme colour name as the stock card takes, or a hex value; the rest
keep the names the statistics carry and take the theme's graph palette
in order:

```yaml
states:
  - state: heat
    name: Heating
    color: deep-orange
  - state: cool
    color: "#03a9f4"
  - "off"
```

The editor lists the entity's states with a tick, a drag handle, a name
and a colour each, and writes the two keys for you. Its "Ignore states that
appear later" switch chooses which the unticked states become: with it
on they are left out of `states:`; with it off they go in
`ignore_states:`, which stays present — empty if need be — so the list
stays open.

`unit: percent` is the share of each period spent in the state, so a
stacked bar whose states are all drawn is always full height — except the
last bar, which is only as full as the period it covers so far. `auto`
picks hours for hourly and daily periods and days for coarser ones.

`chart_type` takes the stock statistics-graph card's four values, so a
config moves between the two cards. A line is drawn through each period's
start, as the stock card draws it.

`hide_legend: true` leaves the legend off.

`energy_date_selection: true` makes the card follow a dashboard's
`energy-date-selection` card instead of `days_to_show`; `collection_key`
names the picker when a dashboard has more than one.

The card asks the integration for its buckets rather than the recorder.
The sums are cumulative and dense, so a period's value is the difference
between the rows at its two edges: a year of months is thirteen rows a
state, not every hour of the year reduced on the server, and the card
loads in the time it takes to draw. A gap in the statistics — downtime
longer than the recorder keeps — is time in no state, so the bars either
side of it are shorter by exactly the time it took from them.

The card renders through Home Assistant's own chart component. Because
that component is internal to the frontend, a Home Assistant release can
change it; the integration's minimum version is raised when that happens.

## Period sensors

A sensor with a number in it: how long the door was open today, what share
of the month the heat pump spent heating, how many times the garage opened
this year. Each reads its value out of the statistics above, so it
reaches back as far as they do, and the recorder's retention does not
shrink it.

They are opt-in, one at a time. On the integration's page, open the entry
for the entity and choose **Add sensor**:

- **States** — one or more, added together, from the states the entity has
  statistics for and the options of an enum sensor; any state can be typed
  in. A state the entry's own settings ignore is refused, since a sensor
  over it would never move. Leave it empty to count changes between every
  state.
- **Measure** — time in the states in hours, the share of the period spent
  in them as a percentage, or the number of changes into them.
- **Period** — today, yesterday, this week, last week, this month, last
  month, this year, last year, or all time. Weeks start on Monday, days at
  midnight in Home Assistant's own time zone.
- **Name** — optional; the default is made from the entity, the states,
  the measure and the period, "Front Door open time this month".
- **Include the current hour** — statistics are compiled hourly. On, the
  sensor follows the entity between compiles, reading the recorder for the
  hours since the last compiled one and updating on every change and once
  a minute, exactly as the next compile will record them — a state that
  has to last a minimum duration is left out until it has. Off, the sensor
  moves once an hour and is a pure function of the statistics.

The sensor belongs to the entry: its settings are edited from the entry's
page and deleting it there removes the sensor. The entry itself still has
no entities.

A time sensor is a duration in hours with two decimals, a share a
percentage with one, a count a whole number. Each carries `period_start`
and `period_end`, `compiled_until` — the end of the last compiled hour,
which is where the statistics stop and the live reading starts — and
`live`. A period that starts before the entity's statistics do is measured
from where they start, and the share is of the time actually measured, so
a sensor over all time on an entity with a month of statistics reads the
share of that month. A sensor whose states are all ignored by the entry,
or whose entity has no statistics yet, is `unavailable`, with the reason in
the log.

**Count.** A count is the number of changes *into* the states inside the
period, as the statistics record them. A spell already in progress when
the period opens is not a change: a door open since yesterday evening
counts `0` openings today until it closes and opens again.
`history_stats` counts it as `1`, because it counts the spells overlapping
its window rather than the changes inside it. For the same reason the
count of a period is exactly the sum of its hours' counts, which is what
lets today's count agree with the chart.

**Keep them out of the recorder.** The sensors change every minute, and
each change is a row in the recorder's `states` table — recording a
number that is derived from statistics the recorder already keeps. The
entity IDs all start with `sensor.discrete_`, so one line excludes them:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.discrete_*
```

The IDs are `sensor.discrete_<entity>_<states>_<measure>_<period>` —
`sensor.discrete_binary_sensor_front_door_on_duration_today` — so the
sensor and the statistic it reads visibly match; renaming the entity ID
afterwards is fine, the exclude is only a convenience. Nothing here adds a
`state_class`, so the recorder does not build a second set of long-term
statistics over these either.

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
into statistics that outlive the recorder. Each is the right tool for a
specific job.

The two are built the other way round from each other. `history_stats` is
sensor first: the number is what it makes, and long-term statistics of it
are optional, a `state_class` on the sensor for the recorder to sum. This
component is statistics first: the hourly rows are the product, and a
sensor over them is optional, a period sensor on the entry. That order is
what lets the recorder's retention be short — a few days is enough, since
the statistics are compiled from the history while it is still there — and
what makes a long range cheap: a year is twelve rows a state, not a year of
state changes read back.

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

**Any window.** The last thirty minutes, since sunrise, until 4 pm:
whatever a template can render. The period sensors here have nine named
periods, each aligned to the clock, and the charts hourly buckets.

**Seconds.** A `history_stats` window can start and end at any second, and
its value is exact to the second within it. A period sensor is exact too,
but only for the named periods, and its chart is hourly.

Its `ratio` type is not on that list. Hours per hour is already a fraction:
the `mean` of a duration statistic over any period *is* the share of that
period spent in the state - a `mean` of `0.4` is 40 % - and it is what the last
chart under *Charts* draws.

### Side by side

| | `history_stats` | `discrete_statistics` |
|---|---|---|
| Produces | one sensor: a value for the current window | per-state duration and count statistics, per hour; period sensors over them |
| Freshness | on change, at least every minute | statistics after each hour closes; a period sensor on change and every minute |
| Resolution | seconds, within the window | hourly buckets |
| Reach into the past | as far as the recorder's retention | whole retained history on first run, kept forever after |
| Backfill | none — begins when the sensor is created | first run, and `recompute` for any range with history |
| After purge | window silently shrinks toward `0` | statistics unaffected |
| States per entity | one set per sensor, merged into one number | every state, automatically |
| A new state | a new sensor, when you notice | recorded from its first hour |
| Count means | intervals in the window; a state active at the start counts | transitions into the state, in the hour they happen |
| `unavailable` / `unknown` | not in the list, so they break the interval | carry the previous state forward; configurable |
| State mapping | none | `states:` map, `default`, `blank` |
| Window | any template; two of `start`/`end`/`duration` | today, yesterday, this/last week, month, year, all time |
| Share of time | `ratio` % | `share` sensor, or the `mean` of a duration: hours per hour is a fraction |
| Debounce | `min_state_duration` | `ignore_short` with `min_duration`, per state or as the default |
| Usable in automations | yes, it is a sensor | yes, a period sensor |
| Configuration | UI with live preview, or YAML; one sensor per state × metric × window | UI or YAML; one entry per entity, sensors added to it |
| Long-term statistics | of the sensor's own value (`measurement`), or `total_increasing` with reset detection | are the product |

Use `history_stats` for a window only a template can say; this component
for the history, the chart, and the numbers over named periods that a
purge cannot shrink.

## Limitations

- Hourly buckets only. The external statistics API writes only to the
  hourly table.
- An in-progress state change is not charted until its hour closes; a
  period sensor with the current hour included shows it.
- A state committed later than the trailing window needs a manual
  `recompute`.
- The stock statistics-graph card names its statistics explicitly, so a
  newly appearing state must be added to it by hand; the integration's own
  card draws it as soon as it has statistics.
- Hours the component was not running for, beyond the recorder's
  `purge_keep_days`, are recorded only when its own last row can vouch for
  the state; otherwise they stay empty (see *Gaps*).
