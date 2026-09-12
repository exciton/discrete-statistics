# The bench

The harness behind the numbers in [`docs/performance.md`](../docs/performance.md),
pointed at your own Home Assistant database. It measures the card's read,
the period sensors' read, the compile, and Home Assistant's own
`statistics_during_period` and `history_stats` asked the same questions —
on SQLite, Postgres and MariaDB alike — and records every answer so a
change can be shown to have altered cost and not results.

It is not part of the integration and not part of `script/test tests/`:
`pytest.ini` collects `tests` only, and the bench needs a prepared
database and a cases file before it means anything.

**The step-by-step instructions are in
[docs/performance.md, "Reproducing this"](../docs/performance.md#reproducing-this).**
In brief:

```bash
script/bench-extract backup.tar bench/data      # a database out of a backup
cp bench/cases.example.yaml bench/cases.yaml    # what to measure; then edit
script/bench live sqlite measure                # read it
python3 bench/compare.py A.json B.json          # two runs, side by side
```

| file | what it is |
| --- | --- |
| `cases.example.yaml` | every field the cases document takes, commented |
| `cases.py` | the cases document as typed values; pure |
| `harness.py` | the measurement: the meter, the modes, the EXPLAIN capture |
| `test_bench.py` | the pytest driver, one test per mode |
| `test_selftest.py` | the harness against a database it builds itself — `script/bench selftest` |
| `conftest.py` | the fixtures, and the guard that keeps `test_bench.py` uncollected without a database |
| `load.py` | SQLite → MariaDB/Postgres, with verification |
| `extract.py` | a backup → a database and `entries.json` |
| `compare.py` | two result files side by side, cost and answers |
| `summarize.py` | a matrix of result files → markdown tables |

Paths come from the environment, so nothing needs editing to point the
bench elsewhere: `BENCH_CASES` (default `bench/cases.yaml`), `BENCH_DATA`
(`bench/data` — `entries.json`, and `<variant>/home-assistant_v2.db`) and
`BENCH_RESULTS` (`bench/results`, git-ignored). `script/bench` writes the
rest into `$BENCH_RESULTS/run.json` for the container to read.
