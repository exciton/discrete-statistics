#!/usr/bin/env python3
"""Pull a benchmark database out of a Home Assistant backup.

    python3 bench/extract.py BACKUP.tar OUTDIR [--variant NAME] [--key FILE]

Writes `OUTDIR/<variant>/home-assistant_v2.db` and `OUTDIR/entries.json` -
the layout `BENCH_DATA` points at. `entries.json` is this integration's
config entries, taken from the backup's `.storage/core.config_entries`,
which is where the harness gets each entity's settings.

A Supervisor backup is a tar holding `homeassistant.tar.gz`. When the
backup is encrypted that inner archive is a securetar stream and needs the
backup's encryption key: put it in a file and pass `--key`. `backup.json`
says which it is, and an encrypted backup without a key is refused rather
than left to fail halfway through six gigabytes.

Runs in the bench image, which carries `securetar`.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path

DOMAIN = "discrete_statistics"
DATABASE = "home-assistant_v2.db"
# The write-ahead log holds committed transactions the main file does not;
# without it a copy can be missing the most recent hours.
WANTED_SUFFIXES = (DATABASE, f"{DATABASE}-wal", f"{DATABASE}-shm")
ENTRIES = ".storage/core.config_entries"


def _protected(backup: Path) -> bool:
    with tarfile.open(backup) as outer:
        member = outer.extractfile("./backup.json")
        if member is None:
            return False
        return bool(json.loads(member.read()).get("protected", False))


def _inner(outer: tarfile.TarFile, backup: Path, password: str | None):
    """The `homeassistant.tar.gz` member, decrypted when it has to be."""
    stream = outer.extractfile("homeassistant.tar.gz")
    if stream is None:
        raise SystemExit("no homeassistant.tar.gz in the backup")
    if password is None:
        # Streaming: the member is not seekable inside the outer tar.
        return tarfile.open(fileobj=stream, mode="r|gz")
    from securetar import SecureTarFile

    return SecureTarFile(backup, gzip=True, password=password, fileobj=stream)


def _write_entries(raw: bytes, out: Path) -> int:
    storage = json.loads(raw)
    entries = [
        entry for entry in storage["data"]["entries"] if entry.get("domain") == DOMAIN
    ]
    (out / "entries.json").write_text(json.dumps(entries, indent=1))
    return len(entries)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("backup", help="a Home Assistant (Supervisor) backup tar")
    ap.add_argument("outdir", help="where to write; point BENCH_DATA here")
    ap.add_argument(
        "--variant",
        default="live",
        help="the subdirectory the database lands in, and the label"
        " `script/bench <variant> sqlite` measures it under",
    )
    ap.add_argument("--key", help="file holding the backup's encryption key")
    args = ap.parse_args()

    backup = Path(args.backup)
    out = Path(args.outdir)
    database_dir = out / args.variant
    database_dir.mkdir(parents=True, exist_ok=True)

    password = Path(args.key).read_text().strip() if args.key else None
    if password is None and _protected(backup):
        print(
            f"{backup} is encrypted; pass --key with the backup's encryption key",
            file=sys.stderr,
        )
        return 1

    found: list[str] = []
    entries = None
    with tarfile.open(backup) as outer, _inner(outer, backup, password) as inner:
        for member in inner:
            name = member.name
            if name.endswith(WANTED_SUFFIXES):
                target = database_dir / Path(name).name
                source = inner.extractfile(member)
                assert source is not None
                with open(target, "wb") as fh:
                    while chunk := source.read(1 << 22):
                        fh.write(chunk)
                found.append(target.name)
                print(f"extracted {target} ({member.size} bytes)", flush=True)
            elif name.endswith(ENTRIES):
                source = inner.extractfile(member)
                assert source is not None
                entries = source.read()

    if DATABASE not in found:
        print("no database in the backup", file=sys.stderr)
        return 1
    if entries is None:
        print(
            f"warning: no {ENTRIES} in the backup; write entries.json by hand",
            file=sys.stderr,
        )
    else:
        count = _write_entries(entries, out)
        print(f"wrote {out / 'entries.json'} ({count} entries)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
