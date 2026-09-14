#!/usr/bin/env python3
"""Aggregate a matrix of bench results into markdown tables. Host-runnable.

    python3 bench/summarize.py [--results DIR] [--stamp PREFIX]
                               [--baseline BRANCH] [-o FILE]

A matrix is whatever result files the directory holds: one per
branch/engine/variant, named as the harness writes them. The newest file
of each combination wins, and `--stamp` restricts that to one run of the
matrix by timestamp prefix (`20260912T06`).

Three sections: whether the branches answer alike, what each group of
cases costs per engine and database, and the card's read against the
stock command case by case.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict

# The case-name prefixes the harness writes, and what each group is.
GROUPS = [
    ("buckets", "our `buckets` command"),
    ("stock", "stock `statistics_during_period`"),
    ("ws ours", "our command through the websocket"),
    ("ws stock", "the stock command through the websocket"),
    ("sensor", "a period sensor's warm refresh"),
    ("hstats", "`history_stats`, cold"),
    ("psensor", "a period sensor's cold first refresh"),
    ("frame", "`async_compiled` per entity"),
]


def collect(results_dir: str, stamp: str) -> dict[tuple[str, str, str], dict]:
    """The newest result file per (branch, engine, variant)."""
    runs: dict[tuple[str, str, str], dict] = {}
    for path in sorted(glob.glob(os.path.join(results_dir, "*.json"))):
        if stamp and stamp not in os.path.basename(path):
            continue
        with open(path) as fh:
            data = json.load(fh)
        if data.get("mode", "measure") != "measure":
            continue
        key = (data["branch"], data.get("engine", "sqlite"), data["variant"])
        runs[key] = data  # sorted by name, so the newest stamp wins
    return runs


def rows(run: dict) -> dict[str, dict]:
    return {r["case"]: r for r in run["results"]}


def group(run: dict, prefix: str) -> list[dict]:
    if prefix == "ws ours":
        return [r for r in run["results"] if r["case"].startswith("ws ours")]
    if prefix == "ws stock":
        return [r for r in run["results"] if r["case"].startswith("ws stock")]
    return [
        r
        for r in run["results"]
        if r["case"].startswith(prefix) and not r["case"].startswith("ws ")
    ]


def total(rs: list[dict], key: str = "median_ms") -> float:
    return sum(r[key] for r in rs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="bench/results")
    ap.add_argument("--stamp", default="", help="timestamp prefix, e.g. 20260912T06")
    ap.add_argument(
        "--baseline", default="", help="branch every other is diffed against"
    )
    ap.add_argument("-o", "--out", default="", help="also write the markdown here")
    args = ap.parse_args()

    runs = collect(args.results, args.stamp)
    if not runs:
        raise SystemExit(f"no measure results in {args.results}")
    branches = sorted({k[0] for k in runs})
    engines = sorted({k[1] for k in runs})
    variants = sorted({k[2] for k in runs})
    baseline = args.baseline or branches[0]

    out: list[str] = []
    P = out.append
    P("# Benchmark matrix\n")
    some = next(iter(runs.values()))
    P(
        f"{len(runs)} runs: {', '.join(branches)} x {', '.join(engines)} x "
        f"{', '.join(variants)}. Home Assistant {some['ha_version']}, "
        f"{some['repeat']} repeats after a warm-up, median ms.\n"
    )

    P(f"## 1. Answers, against `{baseline}`\n")
    differences: list[tuple[str, str, str, str]] = []
    compared = 0
    for engine in engines:
        for variant in variants:
            base = runs.get((baseline, engine, variant))
            if base is None:
                continue
            base_rows = rows(base)
            for branch in branches:
                other = runs.get((branch, engine, variant))
                if branch == baseline or other is None:
                    continue
                for case, row in base_rows.items():
                    if row["check"] is None or case.startswith(("hstats", "psensor")):
                        continue
                    mine = rows(other).get(case)
                    if mine is None:
                        continue
                    compared += 1
                    if mine["check"] != row["check"]:
                        differences.append((branch, engine, variant, case))
    if len(branches) == 1:
        P(f"- Only `{baseline}` was measured: nothing to diff against.")
    else:
        P(
            f"- {compared} answers compared (buckets, stock, sensors, "
            f"watermarks): **{len(differences) or 'none'} differ**."
        )
    for branch, engine, variant, case in differences[:20]:
        P(f"  - {branch} {engine}/{variant}: `{case}`")
    if len(differences) > 20:
        P(f"  - ... and {len(differences) - 20} more")

    agreement: dict[str, int] = defaultdict(int)
    for run in runs.values():
        for row in group(run, "buckets"):
            for key, value in (row.get("agreement") or {}).items():
                agreement[key] += value
    if agreement:
        P(
            f"- Against the stock `statistics_during_period`: "
            f"{agreement['agree']} bucket values compared, "
            f"**{agreement['disagree']} disagree**, "
            f"{agreement['extra_zero']} zero buckets only we return."
        )
    pairs = {
        (
            row["case"][8:],
            round(row["check"]["hstats"], 2),
            round(row["check"]["psensor"], 2)
            if row["check"]["psensor"] is not None
            else None,
        )
        for run in runs.values()
        for row in group(run, "hstats")
        if row.get("check")
    }
    if pairs:
        P("- `history_stats` against a period sensor:")
        for name, left, right in sorted(pairs):
            P(f"  - {name}: history_stats {left}, sensor {right}")
    P("")

    P("## 2. Cost per group\n")
    P("Totals over a group's cases: ms / statements / rows fetched.\n")
    for variant in variants:
        P(f"### {variant}\n")
        P("| group | engine | " + " | ".join(branches) + " |")
        P("|---|---|" + "---|" * len(branches))
        for prefix, _what in GROUPS:
            for engine in engines:
                cells = []
                for branch in branches:
                    run = runs.get((branch, engine, variant))
                    rs = group(run, prefix) if run else []
                    cells.append(
                        f"{total(rs):.0f} / {total(rs, 'statements'):.0f} /"
                        f" {total(rs, 'rows'):.0f}"
                        if rs
                        else "-"
                    )
                if any(c != "-" for c in cells):
                    P(f"| `{prefix}` | {engine} | " + " | ".join(cells) + " |")
        P("")

    P(f"## 3. The card: `buckets` against `stock`, per case ({baseline})\n")
    for variant in variants:
        run = runs.get((baseline, engines[0], variant))
        if run is None:
            continue
        names = [r["case"][8:] for r in group(run, "buckets")]
        P(f"### {variant}\n")
        P("| case | " + " | ".join(f"{e}: stock -> ours" for e in engines) + " |")
        P("|---|" + "---|" * len(engines))
        for name in names:
            cells = []
            for engine in engines:
                this = runs.get((baseline, engine, variant))
                if this is None:
                    cells.append("-")
                    continue
                r = rows(this)
                stock, ours = r.get("stock   " + name), r.get("buckets " + name)
                cells.append(
                    f"{stock['median_ms']:.1f} / {stock['rows']} -> "
                    f"**{ours['median_ms']:.1f}** / {ours['rows']}"
                    if stock and ours
                    else "-"
                )
            P(f"| {name} | " + " | ".join(cells) + " |")
        P("")

    text = "\n".join(out) + "\n"
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
