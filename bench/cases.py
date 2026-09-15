"""What the bench measures, read from a file rather than compiled in.

The harness knows nothing about any particular install: the entities, the
card cases, the sensors and the `history_stats` pairs all come from a
YAML (or JSON) document the user writes. `cases.example.yaml` documents
every field.

Pure: no Home Assistant instance and no recorder, so `bench/test_selftest.py`
can build a `Cases` in memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from custom_components.discrete_statistics.const import METRIC_DURATION
from custom_components.discrete_statistics.reading import Spec
from custom_components.discrete_statistics.statistic_ids import build as build_sid

# A pair's answers are reported, never asserted: the two sides read
# different tables and a disagreement is the finding, not a failure.
TIME_TOLERANCE_HOURS = 0.05
RATIO_TOLERANCE = 0.2


@dataclass(frozen=True)
class BucketCase:
    """One chart, as the card would ask for it."""

    name: str
    statistic_ids: tuple[str, ...]
    days: float
    period: str
    # Also asked through the real websocket, so transport and JSON count.
    websocket: bool = False
    # Also asked of the stock `statistics_during_period`, for the answer diff.
    stock: bool = True


@dataclass(frozen=True)
class SensorCase:
    """One period sensor, as its subentry configures it."""

    name: str
    entity_id: str
    spec: Spec


@dataclass(frozen=True)
class HistoryPair:
    """The same question put to `history_stats` and to a period sensor.

    `kind` is what `history_stats` would be configured as - time, count or
    ratio - and `metric` the sensor's equivalent.
    """

    name: str
    entity_id: str
    states: tuple[str, ...]
    kind: str
    metric: str
    period: str
    show_sql: bool = False


@dataclass(frozen=True)
class Cases:
    time_zone: str
    repeat: int
    # The entities `build` and `compile` walk and whose frame `measure`
    # times; empty means every configured one.
    entities: tuple[str, ...]
    buckets: tuple[BucketCase, ...]
    sensors: tuple[SensorCase, ...]
    history: tuple[HistoryPair, ...]
    time_tolerance_hours: float = TIME_TOLERANCE_HOURS
    ratio_tolerance: float = RATIO_TOLERANCE


def _states(case: dict[str, Any]) -> tuple[str, ...]:
    """The states a case names.

    YAML 1.1 reads a bare `on` or `off` as a boolean, and those are the two
    commonest states there are, so they are read back rather than left to
    fail as `True`.
    """
    return tuple(
        {True: "on", False: "off"}[state] if isinstance(state, bool) else str(state)
        for state in case.get("states") or ()
    )


def _ids(case: dict[str, Any]) -> tuple[str, ...]:
    """A case names its statistics outright, or an entity and its states."""
    if given := case.get("statistic_ids"):
        return tuple(given)
    entity_id = case["entity"]
    metric = case.get("metric", METRIC_DURATION)
    return tuple(build_sid(entity_id, state, metric) for state in _states(case))


def _bucket(case: dict[str, Any]) -> BucketCase:
    return BucketCase(
        name=case["name"],
        statistic_ids=_ids(case),
        days=float(case["days"]),
        period=case["period"],
        websocket=bool(case.get("websocket", False)),
        stock=bool(case.get("stock", True)),
    )


def _sensor(case: dict[str, Any]) -> SensorCase:
    return SensorCase(
        name=case["name"],
        entity_id=case["entity"],
        spec=Spec(
            _states(case),
            case.get("metric", METRIC_DURATION),
            case["period"],
            bool(case.get("live", True)),
        ),
    )


def _pair(case: dict[str, Any]) -> HistoryPair:
    return HistoryPair(
        name=case["name"],
        entity_id=case["entity"],
        states=_states(case),
        kind=case["kind"],
        metric=case.get("metric", METRIC_DURATION),
        period=case["period"],
        show_sql=bool(case.get("show_sql", False)),
    )


def from_dict(document: dict[str, Any]) -> Cases:
    return Cases(
        time_zone=document.get("time_zone", "UTC"),
        repeat=int(document.get("repeat", 5)),
        entities=tuple(document.get("entities") or ()),
        buckets=tuple(_bucket(c) for c in document.get("bucket_cases") or ()),
        sensors=tuple(_sensor(c) for c in document.get("sensors") or ()),
        history=tuple(_pair(c) for c in document.get("history_pairs") or ()),
        time_tolerance_hours=float(
            document.get("time_tolerance_hours", TIME_TOLERANCE_HOURS)
        ),
        ratio_tolerance=float(document.get("ratio_tolerance", RATIO_TOLERANCE)),
    )


def load(path: str | Path) -> Cases:
    """Read a cases document. YAML is a superset of JSON, so one parser does."""
    text = Path(path).read_text()
    return from_dict(yaml.safe_load(text) or {})


def entries(data_dir: str | Path) -> list[dict[str, Any]]:
    """The config entries the harness configures its entities from.

    `script/bench-extract` writes this out of a backup's
    `.storage/core.config_entries`; the format is that file's `entries`
    list, filtered to this integration's domain.
    """
    return json.loads((Path(data_dir) / "entries.json").read_text())
