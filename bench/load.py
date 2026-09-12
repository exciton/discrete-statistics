#!/usr/bin/env python3
"""Copy a bench SQLite database into MariaDB or Postgres.

    python3 bench/load.py --source <sqlite file> --target <url> \
        --entries <entries.json> [--states]

Only what the bench reads is copied: `statistics_meta`, `statistics`,
`recorder_runs`, `schema_changes`, and - with `--states`, for `build` and
`compile` modes - `states_meta`, `state_attributes` and `states`
restricted to the entities `entries.json` configures.
`statistics_short_term` (tens of millions of rows on a real install),
events and everything else are skipped.

The schema is Home Assistant's own, made by booting the recorder against
an empty database (`script/bench <variant> <engine> schema`); this only
fills it. Ids are preserved exactly - `statistics.metadata_id`,
`states.metadata_id`, `old_state_id`, `attributes_id` - so the two
engines answer from the same graph the SQLite file holds.

Runs in the bench image (`ha-discrete-stats-bench`), which carries
pymysql and psycopg2.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

CHUNK = 500_000  # rows per LOAD DATA file / COPY batch

# Loaded in this order; deleted in reverse, which is the order the foreign
# keys allow.
STATS_TABLES = ["schema_changes", "recorder_runs", "statistics_meta", "statistics"]
STATE_TABLES = ["states_meta", "state_attributes", "states"]


# --------------------------------------------------------------- encoding

# The text format MySQL's LOAD DATA and Postgres' COPY both default to:
# tab-separated, backslash-escaped, `\N` for NULL. One encoder serves both.
_ESCAPES = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"})


def _field(value, binary: bool, pg: bool) -> str:
    if value is None:
        return "\\N"
    if isinstance(value, (bytes, bytearray, memoryview)):
        hexed = bytes(value).hex()
        # Postgres parses a bytea in text COPY through bytea's own input,
        # which wants a literal `\x...`; MySQL gets plain hex and UNHEXes
        # it in the LOAD DATA column list.
        return ("\\\\x" + hexed) if pg else hexed
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    return str(value).translate(_ESCAPES)


def _line(row, binaries: list[bool], pg: bool) -> str:
    return "\t".join(_field(v, b, pg) for v, b in zip(row, binaries)) + "\n"


class _Reader:
    """A read-only file over an iterator of strings, for `copy_expert`."""

    def __init__(self, lines) -> None:
        self._lines = iter(lines)
        self._buf = ""

    def read(self, size: int = -1) -> str:
        while size < 0 or len(self._buf) < size:
            try:
                self._buf += next(self._lines)
            except StopIteration:
                break
        if size < 0 or len(self._buf) <= size:
            out, self._buf = self._buf, ""
            return out
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def readline(self, size: int = -1) -> str:
        if "\n" not in self._buf:
            for line in self._lines:
                self._buf += line
                if "\n" in self._buf:
                    break
        head, sep, tail = self._buf.partition("\n")
        self._buf = tail
        return head + sep


# --------------------------------------------------------------- targets


class Target:
    """What the loader needs of an engine: columns, truncate, bulk load."""

    def columns(self, table: str) -> list[tuple[str, bool]]:
        raise NotImplementedError


class MySQL(Target):
    name = "mariadb"

    def __init__(self, url: str) -> None:
        import pymysql
        from sqlalchemy.engine import make_url

        u = make_url(url)
        self.db = u.database
        self.con = pymysql.connect(
            host=u.host,
            port=u.port or 3306,
            user=u.username,
            password=u.password,
            database=u.database,
            charset="utf8mb4",
            local_infile=True,
            autocommit=False,
        )
        with self.con.cursor() as cur:
            # Ids are preserved verbatim, `states.old_state_id` included,
            # and it may point at a state of an entity we did not copy.
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            cur.execute("SET UNIQUE_CHECKS=0")
            cur.execute("SET SESSION sql_mode=''")
            # A DELETE of 9M InnoDB rows outlives the default lock wait;
            # `clear` truncates instead, and this covers a reload that
            # falls back to DELETE on a small table.
            cur.execute("SET SESSION innodb_lock_wait_timeout=600")

    def columns(self, table):
        with self.con.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (self.db, table),
            )
            return [(c, t.endswith("blob")) for c, t in cur.fetchall()]

    def clear(self, tables):
        # TRUNCATE, not DELETE: DELETE of 9M InnoDB rows takes longer than
        # the load does. FOREIGN_KEY_CHECKS is already off, which is what
        # lets a table another table references be truncated at all.
        with self.con.cursor() as cur:
            for table in reversed(tables):
                cur.execute(f"TRUNCATE TABLE `{table}`")
        self.con.commit()

    def load(self, table, columns, binaries, rows):
        # LOAD DATA LOCAL INFILE wants a file; the rows are written out in
        # chunks so a 9M-row table never needs a 9M-row temp file.
        targets, sets = [], []
        for column, binary in zip(columns, binaries):
            if binary:
                targets.append(f"@v_{column}")
                sets.append(f"`{column}`=UNHEX(@v_{column})")
            else:
                targets.append(f"`{column}`")
        clause = f" SET {', '.join(sets)}" if sets else ""
        total = 0
        for chunk in _chunks(rows, CHUNK):
            with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=True) as fh:
                for row in chunk:
                    fh.write(_line(row, binaries, pg=False))
                fh.flush()
                with self.con.cursor() as cur:
                    cur.execute(
                        f"LOAD DATA LOCAL INFILE '{fh.name}' INTO TABLE `{table}`"
                        f" ({', '.join(targets)}){clause}"
                    )
            total += len(chunk)
        self.con.commit()
        return total

    def finish(self, tables):
        with self.con.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
            cur.execute("SET UNIQUE_CHECKS=1")
            for table in tables:
                cur.execute(f"ANALYZE TABLE `{table}`")
        self.con.commit()

    def scalars(self, sql):
        with self.con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


class Postgres(Target):
    name = "postgres"

    def __init__(self, url: str) -> None:
        import psycopg2
        from sqlalchemy.engine import make_url

        u = make_url(url)
        self.con = psycopg2.connect(
            host=u.host,
            port=u.port or 5432,
            user=u.username,
            password=u.password,
            dbname=u.database,
        )
        with self.con.cursor() as cur:
            # Same reason as MySQL's FOREIGN_KEY_CHECKS: `old_state_id`
            # is kept as it is and may dangle.
            cur.execute("SET session_replication_role = replica")
        self.con.commit()

    def columns(self, table):
        with self.con.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                (table,),
            )
            return [(c, t == "bytea") for c, t in cur.fetchall()]

    def clear(self, tables):
        # TRUNCATE needs CASCADE for the referenced tables; the whole set
        # is being replaced, so cascading to it is exactly right.
        with self.con.cursor() as cur:
            for table in reversed(tables):
                cur.execute(f'TRUNCATE TABLE "{table}" CASCADE')
        self.con.commit()

    def load(self, table, columns, binaries, rows):
        cols = ", ".join(f'"{c}"' for c in columns)
        total = 0
        with self.con.cursor() as cur:
            for chunk in _chunks(rows, CHUNK):
                cur.copy_expert(
                    f'COPY "{table}" ({cols}) FROM STDIN',
                    _Reader(_line(row, binaries, pg=True) for row in chunk),
                )
                total += len(chunk)
        self.con.commit()
        return total

    def finish(self, tables):
        # The id columns are identities, not sequences; a load that keeps
        # the source ids leaves them at 1, so the next insert collides.
        identity = {
            "statistics": "id",
            "statistics_meta": "id",
            "states": "state_id",
            "states_meta": "metadata_id",
            "state_attributes": "attributes_id",
            "recorder_runs": "run_id",
            "schema_changes": "change_id",
        }
        with self.con.cursor() as cur:
            cur.execute("SET session_replication_role = origin")
            for table in tables:
                column = identity[table]
                cur.execute(f'SELECT COALESCE(MAX("{column}"), 0) + 1 FROM "{table}"')
                cur.execute(
                    f'ALTER TABLE "{table}" ALTER COLUMN "{column}"'
                    f" RESTART WITH {cur.fetchone()[0]}"
                )
                cur.execute(f'ANALYZE "{table}"')
        self.con.commit()

    def scalars(self, sql):
        with self.con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def _chunks(iterator, size):
    chunk = []
    for row in iterator:
        chunk.append(row)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


# --------------------------------------------------------------- the copy


def _entities(entries: str) -> list[str]:
    """The entities whose `states` rows the bench needs, from `entries.json`."""
    return [e["data"]["entity_id"] for e in json.loads(Path(entries).read_text())]


def _source_columns(con, table) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument(
        "--entries",
        required=True,
        help="entries.json, naming the entities whose states are copied",
    )
    ap.add_argument(
        "--states",
        action="store_true",
        help="also copy those entities' states, which `build` and `compile` need",
    )
    ap.add_argument(
        "--only",
        choices=("stats", "states"),
        help="load just one half, leaving the other as it is; the statistics"
        " half is the expensive one, so a run that only needs states added"
        " does not pay for it again",
    )
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    target = (
        Postgres(args.target)
        if args.target.startswith("postgres")
        else MySQL(args.target)
    )
    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    src.execute("PRAGMA query_only=1")

    entities = _entities(args.entries)
    holes = ",".join("?" * len(entities))
    ids = [
        r[0]
        for r in src.execute(
            f"SELECT metadata_id FROM states_meta WHERE entity_id IN ({holes})",
            entities,
        )
    ]
    print(
        f"{len(ids)} of the {len(entities)} configured entities have states_meta rows",
        flush=True,
    )
    id_list = ",".join(str(i) for i in ids)

    tables = list(STATS_TABLES) + (
        STATE_TABLES if args.states or args.only == "states" else []
    )
    if args.only == "stats":
        tables = list(STATS_TABLES)
    elif args.only == "states":
        tables = list(STATE_TABLES)
    # table -> the WHERE that restricts it to what the bench needs
    where = {
        "states": f"WHERE metadata_id IN ({id_list})",
        "states_meta": f"WHERE metadata_id IN ({id_list})",
        "state_attributes": (
            "WHERE attributes_id IN (SELECT DISTINCT attributes_id FROM states"
            f" WHERE metadata_id IN ({id_list}) AND attributes_id IS NOT NULL)"
        ),
    }

    if not args.verify_only:
        target.clear(tables)
        started = time.perf_counter()
        for table in tables:
            columns = [c for c, _ in target.columns(table)]
            binaries = {c: b for c, b in target.columns(table)}
            available = set(_source_columns(src, table))
            # A database Home Assistant creates today has every column a
            # backup's file has; intersecting keeps the loader honest if it
            # ever does not.
            columns = [c for c in columns if c in available]
            clause = where.get(table, "")
            count = src.execute(f"SELECT COUNT(*) FROM {table} {clause}").fetchone()[0]
            one = time.perf_counter()
            rows = src.execute(f"SELECT {', '.join(columns)} FROM {table} {clause}")
            loaded = target.load(table, columns, [binaries[c] for c in columns], rows)
            print(
                f"{table:<20} {loaded:>10} rows (source {count:>10})"
                f" {time.perf_counter() - one:>8.1f}s",
                flush=True,
            )
            assert loaded == count, f"{table}: loaded {loaded}, source has {count}"
        target.finish(tables)
        print(f"loaded in {time.perf_counter() - started:.1f}s", flush=True)

    return verify(src, target, tables, where)


def verify(src, target, tables, where) -> int:
    """Row counts per table, then per-statistic sums and newest hour."""
    bad = 0
    print("\nverification", flush=True)
    for table in tables:
        want = src.execute(
            f"SELECT COUNT(*) FROM {table} {where.get(table, '')}"
        ).fetchone()[0]
        got = target.scalars(f"SELECT COUNT(*) FROM {table}")[0][0]
        ok = "ok" if want == got else "MISMATCH"
        bad += want != got
        print(
            f"  count {table:<20} source {want:>10}  target {got:>10}  {ok}", flush=True
        )

    sql = (
        "SELECT m.statistic_id, COUNT(*), SUM(s.sum), MAX(s.start_ts)"
        " FROM statistics s JOIN statistics_meta m ON m.id = s.metadata_id"
        " WHERE m.source = 'discrete_statistics' GROUP BY m.statistic_id"
    )
    want = {r[0]: r[1:] for r in src.execute(sql)}
    got = {r[0]: tuple(r[1:]) for r in target.scalars(sql)}
    exact = close = 0
    for key in sorted(set(want) | set(got)):
        a, b = want.get(key), got.get(key)
        if a is None or b is None:
            print(f"  MISSING {key}: source={a} target={b}", flush=True)
            bad += 1
            continue
        if a[0] != b[0] or float(a[2]) != float(b[2]):
            print(f"  MISMATCH {key}: count/max {a} vs {b}", flush=True)
            bad += 1
            continue
        left, right = float(a[1]), float(b[1])
        if repr(left) == repr(right):
            exact += 1
        elif abs(left - right) <= 1e-9 * max(1.0, abs(left)):
            close += 1
            print(f"  within 1e-9 {key}: {left!r} vs {right!r}", flush=True)
        else:
            print(f"  SUM MISMATCH {key}: {left!r} vs {right!r}", flush=True)
            bad += 1
    print(
        f"  {len(want)} statistics of ours: {exact} sums bit-identical, "
        f"{close} within 1e-9, {bad} bad",
        flush=True,
    )
    print("VERIFIED" if not bad else f"FAILED ({bad} problems)", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
