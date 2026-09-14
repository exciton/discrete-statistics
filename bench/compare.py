#!/usr/bin/env python3
"""Side by side comparison of two bench result files. Host-runnable.

    python3 bench/compare.py A.json B.json [--answers N]

Prints the cost table, then the answers that differ: each case's buckets
(start, change), sensor values or watermarks that the two runs disagree
about, up to N entries per case (default 12).

The `note` columns carry whatever a row has beyond its cost: a `buckets`
row's agreement with the stock command, a `ws` row's response size, and
an `hstats`/`psensor` row's two values.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def load(path: str) -> tuple[dict, dict]:
    data = json.loads(Path(path).read_text())
    return data, {row["case"]: row for row in data["results"]}


def when(value: float, ms: bool) -> str:
    return datetime.fromtimestamp(value / (1000 if ms else 1), timezone.utc).strftime(
        "%Y-%m-%d %H:%M"
    )


def note(row: dict) -> str:
    """A row's own finding: agreement, response size, or the pair's values."""
    if agreement := row.get("agreement"):
        return (
            f"agree {agreement['agree']}/{agreement['disagree']}"
            f"/{agreement['extra_zero']}"
        )
    if (size := row.get("response_bytes")) is not None:
        return f"{size} B"
    check = row.get("check")
    if isinstance(check, dict) and "hstats" in check:
        return f"hstats={check['hstats']} psensor={check['psensor']}"
    return ""


def diff_case(case: str, left, right, limit: int) -> list[str]:
    """The entries two answers disagree about, as readable lines."""
    out: list[str] = []
    if left == right:
        return out
    # A `ws` case records no answer, so one side may be None.
    if not isinstance(left, dict) or not isinstance(right, dict):
        return [f"    A={left!r}  B={right!r}"]
    if case.startswith(("buckets", "stock")):
        ms = case.startswith("buckets")
        for sid in sorted(set(left) | set(right)):
            a = {row[0]: row[-1] for row in left.get(sid, [])}
            b = {row[0]: row[-1] for row in right.get(sid, [])}
            for start in sorted(set(a) | set(b)):
                if a.get(start) == b.get(start):
                    continue
                out.append(
                    f"    {sid.split(':')[-1]:<55} {when(start, ms)}  "
                    f"A={a.get(start, 'absent')!s:>14}  B={b.get(start, 'absent')!s:>14}"
                )
    else:
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                out.append(f"    {key:<55} A={left.get(key)!r}  B={right.get(key)!r}")
    if len(out) > limit:
        out = out[:limit] + [f"    ... and {len(out) - limit} more"]
    return out


def main() -> None:
    limit = 12
    args = sys.argv[1:]
    if "--answers" in args:
        i = args.index("--answers")
        limit = int(args[i + 1])
        del args[i : i + 2]
    a, left = load(args[0])
    b, right = load(args[1])
    # A result file without `engine` was SQLite.
    label = lambda d: f"{d['branch']}/{d.get('engine', 'sqlite')}/{d['variant']}"
    print(f"A = {label(a)} ({a['revision']}, {a['our_statistics_rows']} rows of ours)")
    print(f"B = {label(b)} ({b['revision']}, {b['our_statistics_rows']} rows of ours)")
    head = (
        f"{'case':<58} {'A ms':>9} {'B ms':>9} {'x':>6} "
        f"{'A stmt':>7} {'B stmt':>7} {'A rows':>9} {'B rows':>9} {'same':>5}"
        f"  {'A note':<34} {'B note':<34}"
    )
    print(head)
    print("-" * len(head))
    totals = [0.0, 0.0, 0, 0, 0, 0]
    differing: list[tuple[str, list[str]]] = []
    for case, l in left.items():
        r = right.get(case)
        if r is None:
            continue
        ratio = r["median_ms"] / l["median_ms"] if l["median_ms"] else float("nan")
        lines = diff_case(case, l["check"], r["check"], limit)
        if l["check"] is None and r["check"] is None:
            same = "-"  # a case that records no answer, only its cost
        else:
            same = "yes" if l["check"] == r["check"] else "NO"
        if lines:
            differing.append((case, lines))
        print(
            f"{case:<58} {l['median_ms']:>9.2f} {r['median_ms']:>9.2f} {ratio:>6.2f} "
            f"{l['statements']:>7} {r['statements']:>7} "
            f"{l['rows']:>9} {r['rows']:>9} {same:>5}"
            f"  {note(l):<34} {note(r):<34}"
        )
        for i, key in enumerate(("median_ms", "statements", "rows")):
            totals[2 * i] += l[key]
            totals[2 * i + 1] += r[key]
    print("-" * len(head))
    print(
        f"{'TOTAL':<58} {totals[0]:>9.2f} {totals[1]:>9.2f} "
        f"{totals[1] / totals[0] if totals[0] else 0:>6.2f} "
        f"{totals[2]:>7} {totals[3]:>7} {totals[4]:>9} {totals[5]:>9}"
    )
    print()
    if not differing:
        print("answers: identical in every case")
        return
    print(f"answers differ in {len(differing)} case(s):")
    for case, lines in differing:
        print(f"  {case}")
        print("\n".join(lines))


if __name__ == "__main__":
    main()
