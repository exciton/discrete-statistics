# Performance

What this integration stores, how it reads it back, and what both cost. The
numbers below are measurements on one real install, not estimates; how to
take your own is at the end.

## What is stored

For every configured entity, for every state it was in, for every hour:
a cumulative duration in hours and a cumulative count of transitions into
that state, written as external long-term statistics. Two statistics per
state — `<entity>_<state>_duration` and `<entity>_<state>_count` — and one
row per hour that has something to say.

An hour gets a row for a state when that state had time in the hour or was
entered during it, and where a row already stands. Nothing else. A state
an entity has not been in for six months costs nothing over those months;
the states that *were* present in an hour have durations that sum to the
hour, which is the property every chart here rests on.

Rows carry no `mean`, `min` or `max`, only the sum. The recorder's own
reduction skips a row whose mean is `NULL` and never sees the rows that
were not written, so a mean rolled up over a day would be an average over
the hours the state occurred rather than over the day — a light on for one
hour in twenty-four would read as a full hour's mean. The sums are
cumulative and monotonic, so `change` over any span is a subtraction, and
that is what everything reads.

Row volume, on the reference install — 14 entities, 841 statistics, 400
days of history:

| | rows |
|---|---|
| ours, a row only where there is something to record | **181,993** |
| ours, if every state had a row in every hour | 919,088 |
| the whole `statistics` table on that install | 9,202,172 |

So the integration's long-term rows are 19.8% of what a row-per-state-per-hour
scheme would write, and about 2% of a table Home Assistant's own statistics
already fill with 8.3 M rows. Statistics are never purged, by Home Assistant
or by us, so this is the number that grows forever; it is worth it being
small.

## How a chart is read

Because the sums are cumulative, a bucket's value is the difference between
the sum at its two edges. A year of monthly buckets is thirteen edges, so
about thirteen rows per statistic — not 8,760. `recorder/statistics_during_period`
answers the same question by reading every hourly row in the range and
reducing them in Python, which is what makes it read 9,632 rows where we
read 98 for the same chart.

The edges are resolved in one statement per request, whatever the range:

- **SQLite** expands the (statistic, edge) pairs with `json_each` and runs
  one correlated seek on `(metadata_id, start_ts)` per pair. One statement,
  one plan.
- **Postgres** does the same with `jsonb_array_elements`. Expanding matters
  here: Postgres plans every arm of a `UNION ALL` separately, and on a
  13.9 GB database with 2,408 pairs the arm form took 163 ms against the
  expanded form's 15 ms (SQLite: 9 ms against 10 ms).
- **MySQL / MariaDB**, and any engine not recognised, get one literal-bound
  `UNION ALL` arm per pair, batched at 500. `JSON_TABLE` exists on
  MariaDB 10.6+, but MariaDB will not push an outer-referenced bound into a
  range: handed the pairs that way it plans the seek as `ref` on
  `metadata_id` alone and *walks* each series per pair — 477 ms against the
  arms' 35 ms on 472 pairs. The arms are also rendered as text rather than
  built through SQLAlchemy's Core, because constructing five hundred
  subqueries in Python costs more than the server spends answering them
  (472 pairs: 151 ms through the Core, 35 ms as text, of which the server's
  own share is about 40 either way).

The rows come back distinct, without the edges that picked them, and each
edge is resolved by bisect over what came back — a rare state whose single
row answers thirteen edges is one row fetched, not thirteen. At the hourly
period the read is an index range instead, since there every row in the
range answers an edge anyway. A request is bounded at 10,000 buckets before
any of this is walked: the work runs on the recorder's thread.

**Zero and gap are different answers.** A period inside the compiled range
in which a state simply did not occur reads **zero**. A period nothing was
compiled for — downtime longer than the recorder's retention — is left out
of the response, and the card draws a gap. The judgement is made on the
entity's duration statistics *as a whole*, which ride along in every read:
a chart of one rare state must not show a gap in every period the state did
not happen to occur. The stock statistics-graph card cannot make that
distinction, because `statistics_during_period` returns a bucket only where
it found a row: on the same database, 647 buckets that read zero here are
absent there, and a chart drawn from them is holes where the entity was
quiet.

## How a sensor is read

A period sensor's window is read in pieces that never overlap: its whole
compiled hours as the sum at the last edge minus the sum at the first; up
to two part hours, where an edge falls inside an hour; and, for a live
sensor, the hours after the newest compiled one, tallied from the
recorder's raw states.

One coordinator per config entry serves every sensor on it. A refresh
drains the recorder's write queue, then reads:

- the **frame** — which of the entity's statistics exist, the newest
  compiled hour across them, and where the series starts. Each is an index
  seek; two statements per entity.
- **the edges** the sensors between them ask for, minus the ones already
  cached — every remaining edge in one statement, the same read the card
  makes. Cumulative sums do not change behind you, so a finished window's
  edges are cached until a compile writes into the range that contains
  them; a finished window costs no database work at all between the day
  changing and a recompute reaching back to it.
- for a live sensor, **the current hour's** state changes, once, shared by
  every sensor on the entry.

A part hour is answered on evidence: while the recorder still holds that
hour's states, the hour's timeline is tallied and the answer is exact;
afterwards the hour's compiled change is pro-rated by the part inside the
window and the reading is marked `estimated`.

None of this scales with the window. A sensor over 24 hours and a sensor
over 365 days read the same number of rows.

## Compared with `history_stats`

`history_stats` reads the whole window's state changes from `states` on its
first update after a restart or reload, then keeps every one of them in
memory: a state change event appends to the list, a window whose start has
moved forward trims the front, and each update recomputes over the list. It
re-queries only when the window moves *backwards*. That is a good trade for
what it is: after the first read it does no database work at all, and its
answer is exact to the second.

What it costs is memory and startup proportional to the number of changes
in the window, once per sensor, and it cannot answer past `purge_keep_days`
— the rows it needs are gone.

This integration trades the other way. A refresh is a couple of index seeks
per entity plus one statement for the edges, whatever the window; memory is
a handful of floats; and the answer reaches back over the whole compiled
history, which outlives the recorder's retention.

Concretely, on the reference install: a year of a busy light is 1,803
`states` rows for `history_stats` and 122 rows here, every time it must
re-read. A day of the same light is 17 rows there and 122 here. The
crossover is around a thousand state rows — below it `history_stats` is
cheaper on a cold read, above it we are, and warm it reads nothing while we
read the edges.

## Benchmarks

**The install.** 14 entities, 841 statistics, 400 days of history, Home
Assistant 2026.8.3, `America/Los_Angeles`. Three engines: SQLite in-process,
Postgres 16 and MariaDB 11 over the loopback, each holding the same data.
Medians of 5 repeats after a warm-up, on an otherwise idle machine, with
`now` anchored at the newest compiled hour so every run asks the same
questions.

The entities named in the tables:

| in the tables | what it is |
|---|---|
| light A | a busy light: about 1,800 state changes a year |
| light B | a second light, quieter |
| grid status | rare: a handful of outages a year |
| a door | four states, bursty |
| an error sensor | nine states |
| an irrigation zone | on daily |
| a water heater | three states |

**Every answer was identical on every engine**, in every case, and all
22,068 bucket values compared against the stock command agreed — zero
disagreements. The differences are the deliberate ones: quiet buckets read
zero here and are absent from the stock answer.

### (a) The card's read against the stock command

Same statistic IDs, same range, same period, against the sparse rows this
integration writes. `ms / rows fetched`; ours is **2 statements** per case
(one `statistics_meta`, one `statistics`) throughout, the stock command's
two.

| case | SQLite: stock → ours | Postgres: stock → ours | MariaDB: stock → ours |
|---|---|---|---|
| grid status, count, 365 d / month | 4.1 / 25 → **3.8** / 36 | 13.6 / 25 → **5.5** / 36 | 7.9 / 25 → **7.8** / 36 |
| grid status, three counts, 600 d / month | 5.0 / 50 → **4.2** / 45 | 14.4 / 50 → **5.6** / 45 | 8.2 / 50 → **11.7** / 45 |
| a door, four durations, 300 d / week | 31.2 / 9,242 → **5.2** / 188 | 45.8 / 9,242 → **9.9** / 188 | 95.8 / 9,242 → **20.6** / 188 |
| light B, two durations, 10 d / hour | 4.6 / 283 → **8.2** / 287 | 16.8 / 283 → **8.1** / 287 | 14.9 / 283 → **13.2** / 287 |
| light B, one duration, 3 d / hour | 4.8 / 22 → **5.3** / 96 | 6.9 / 22 → **6.6** / 96 | 9.3 / 22 → **9.5** / 96 |
| an error sensor, eight durations, 400 d / week | 29.5 / 9,632 → **13.4** / 98 | 58.0 / 9,632 → **11.3** / 98 | 165.8 / 9,632 → **39.9** / 98 |
| an irrigation zone, one duration, 400 d / month | 3.9 / 225 → **3.4** / 35 | 6.0 / 225 → **12.5** / 35 | 8.9 / 225 → **8.8** / 35 |
| light B, one duration, 365 d / week | 10.3 / 2,275 → **4.6** / 112 | 12.6 / 2,275 → **15.7** / 112 | 31.3 / 2,275 → **15.8** / 112 |

Totals over the eight cases — `ms / statements / rows`:

| engine | stock | ours |
|---|---|---|
| SQLite | 93 / 17 / 21,754 | **48** / 16 / **897** |
| Postgres | 174 / 17 / 21,754 | **75** / 16 / **897** |
| MariaDB | 342 / 17 / 21,754 | **127** / 16 / **897** |

The wide ranges are where it shows — 3–6× on the 300- and 400-day charts.
The hourly cases and the rare-state cases are within a few milliseconds
either way, and one case is a few rows dearer than stock: the grid's
365-day chart fetches 36 rows against stock's 25, the eleven extra being
the entity's sibling duration statistics that decide gap-or-zero, a
question the stock command never answers.

### (b) The same, on a database that still holds dense rows

A row per state per hour — what a database written before the rows went
sparse holds, until the compiler rewrites those hours. Totals over the same
eight cases, `ms / statements / rows`:

| engine | stock | ours |
|---|---|---|
| SQLite | 505 / 17 / 133,787 | **54** / 16 / 1,461 |
| Postgres | 570 / 17 / 133,787 | **65** / 16 / 1,461 |
| MariaDB | 1,301 / 17 / 133,787 | **149** / 16 / 1,461 |

Our own cost barely moves between the two databases; the stock command's
does, by 5–10×, because it reads and reduces every row in the range.

### (c) Through the websocket

The same two charts asked through a real websocket connection, JSON
encoding and transport included:

| engine | a door, 300 d / week | an error sensor, 400 d / week |
|---|---|---|
| SQLite | 32.8 ms, 14,019 B → **8.4 ms**, 14,019 B | 31.9 ms, 6,601 B → **10.2 ms**, 30,667 B |
| Postgres | 57.4 ms, 14,019 B → **31.2 ms**, 14,019 B | 46.4 ms, 6,601 B → **12.6 ms**, 30,667 B |
| MariaDB | 133.2 ms, 14,019 B → **26.1 ms**, 14,019 B | 114.2 ms, 6,601 B → **56.4 ms**, 30,667 B |

The second response is larger than the stock command's, on purpose: those
bytes are the zero buckets — the weeks in which one of the error sensor's
nine states did not occur. The stock command omits them, which is exactly
why a chart drawn from it has holes.

### (d) Period sensors

Six dashboard sensors, warm refresh — the edges-only read the coordinator
makes once the frame and the finished windows are cached. Total over all
six, `ms / statements`:

| engine | sparse rows | dense rows |
|---|---|---|
| SQLite | 26.2 / 6 | 11.9 / 6 |
| Postgres | 18.6 / 6 | 18.3 / 6 |
| MariaDB | 29.8 / 6 | 22.5 / 6 |

Six statements for six sensors, on every engine and both databases.
`history_stats` has no equivalent here: warm, it reads nothing at all.

Cold — `history_stats` constructed fresh against a period sensor's
first-ever refresh, frame and edges and part hours and tail included, same
entity, states and window on both sides. `ms / statements / rows`:

| pair | SQLite | Postgres | MariaDB |
|---|---|---|---|
| light A `on`, this month, time | 9.0 / 2 / 64 → 13.7 / 6 / 122 | 8.6 / 2 / 64 → 20.2 / 6 / 122 | 12.1 / 2 / 64 → 30.9 / 6 / 122 |
| light A `on`, this month, count | 8.4 / 2 / 64 → 16.5 / 6 / 122 | 9.1 / 2 / 64 → 19.6 / 6 / 122 | 10.7 / 2 / 64 → 23.6 / 6 / 122 |
| light B `on`, this week, count | 7.7 / 2 / 54 → 16.9 / 6 / 122 | 8.1 / 2 / 54 → 22.1 / 6 / 122 | 13.1 / 2 / 54 → 34.8 / 6 / 122 |
| light A `on`, last 24 h, time | 10.7 / 2 / 17 → 14.0 / 6 / 122 | 9.6 / 2 / 17 → 22.7 / 6 / 122 | 15.0 / 2 / 17 → 31.3 / 6 / 122 |
| grid status `off`, last 365 d, time | 18.1 / 2 / 686 → **15.5** / 6 / 122 | 17.5 / 2 / 686 → 20.6 / 6 / 122 | 22.9 / 2 / 686 → 23.7 / 6 / 122 |
| light A `on`, last 365 d, time | 37.9 / 2 / 1,803 → **14.9** / 6 / 122 | 32.1 / 2 / 1,803 → **19.7** / 6 / 122 | 50.4 / 2 / 1,803 → **23.6** / 6 / 122 |
| light A `on`, this month, ratio | 7.5 / 2 / 64 → 19.5 / 6 / 122 | 9.0 / 2 / 64 → 27.1 / 6 / 122 | 8.6 / 2 / 64 → 23.7 / 6 / 122 |

Read honestly: on a short window `history_stats` is about twice as cheap
cold — two statements against six, and round trips are most of our time —
and then reads nothing until the window moves backwards. We win the long
windows on a busy entity, and 122 rows is 122 rows whether the window is a
day or a year.

Five of the seven pairs agreed to the last digit. Two differ, both for the
same reason: those entities are configured `default: record_known`, so
`unavailable` carries the surrounding state forward, while `history_stats`
matches the literal state text and charges that time to nobody.

| pair | `history_stats` | period sensor |
|---|---|---|
| grid status `off`, last 365 d, hours | 105.61 | 107.11 |
| light A `on`, last 365 d, hours | 2,738.07 | 2,738.58 |

The raw `states` rows confirm the arithmetic: the grid was `off` for
105.610 h and `unavailable` for 44.785 h, and our extra 1.50 h is the part
of those unavailable spells that sat inside an `off` spell. It is the
documented behaviour of the disposition table, and it grows with the window
on an entity that drops out.

### (e) The frame

`async_compiled` for all 14 configured entities — which statistics each has
and the newest compiled hour across them. The coordinator pays this once
per entry per refresh and caches it:

| engine | sparse rows | dense rows |
|---|---|---|
| SQLite | 123 ms / 28 statements | 141 / 28 |
| Postgres | 137 / 28 | 236 / 28 |
| MariaDB | 175 / 28 | 191 / 28 |

Two statements per entity, whatever the number of statistics it has.

## What it costs to write

Every hour, each entity's trailing window — the last three hours — is
recompiled, so a state change the recorder committed late is picked up and
the recorder's upsert makes the correction invisible. That is the whole
steady-state write cost. Measured on SQLite against the same install:

| | ms | statements |
|---|---|---|
| the hourly run: trailing window, 14 entities | 304 | 390 |
| `recompute` of one whole day, 2 entities | 61 | 158 |

A first compile is the outlier, and it happens once: rebuilding all 14
entities from scratch over the full 400-day retention window took 114
seconds in total. A run is skipped, not queued, while the recorder's own
write queue is deep — compiling is idempotent, so the next run picks up the
same hours.

## Reproducing this

The harness is not shipped with the integration; there is nothing to
install and nothing to run. Everything above is measurable on any install
with the tools already there.

The card's read is the websocket command `discrete_statistics/buckets`,
taking `statistic_ids`, `start_time`, `end_time` and `period`; the
comparison is `recorder/statistics_during_period` with `types: ["change"]`,
the same IDs, the same range and the same period. Both can be sent from
Developer Tools, and both are what the two cards actually send. Time the
round trip, and read the statement and row counts from the database —
SQLite's `sqlite3_trace`, Postgres' `pg_stat_statements`,
MariaDB's general log — with the recorder otherwise idle, since the counts
are engine-wide and not only ours. Take a warm-up pass and then a handful
of repeats, and report the median: the first read of any range pays for the
page cache. Anchor the window at a fixed instant rather than `now`, or
successive runs will not be asking the same question. For the
`history_stats` comparison, give it the same entity, states and window as
literal timestamps rather than templates, and construct it fresh for each
repeat — it caches, and a second reading measures nothing but the cache.
