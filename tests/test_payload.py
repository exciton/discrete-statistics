"""Tests for cumulative sum construction and statistic metadata."""

from datetime import datetime, timezone

import pytest
from homeassistant.components.recorder.models import StatisticMeanType

from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import HOUR
from custom_components.discrete_statistics.payload import (
    build_payloads,
    readable_state,
    rename,
)

T0 = 1767225600.0


def cfg(name=None):
    return EntityConfig(
        entity_id="binary_sensor.grid_status",
        name=name,
        default="record_known",
        states={},
    )


DURATION_ON = "discrete_statistics:binary_sensor_grid_status_on_duration"
COUNT_ON = "discrete_statistics:binary_sensor_grid_status_on_count"
DURATION_OFF = "discrete_statistics:binary_sensor_grid_status_off_duration"
COUNT_OFF = "discrete_statistics:binary_sensor_grid_status_off_count"


def test_single_hour_single_state():
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    metadata, rows, _ = payloads[DURATION_ON]
    assert metadata["name"] == "binary_sensor.grid_status: on (h)"
    assert metadata["source"] == "discrete_statistics"
    assert metadata["statistic_id"] == DURATION_ON
    assert metadata["has_sum"] is True
    assert metadata["unit_of_measurement"] == "h"
    assert metadata["unit_class"] == "duration"
    assert rows == [
        {
            "start": datetime.fromtimestamp(T0, tz=timezone.utc),
            "sum": 1.0,
        }
    ]


def test_count_metadata_has_no_unit():
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 2)}, T0, T0 + HOUR, {})
    metadata, rows, _ = payloads[COUNT_ON]
    assert metadata["unit_of_measurement"] is None
    assert metadata["unit_class"] is None
    assert rows[0]["sum"] == 2


def test_sums_are_cumulative_across_hours():
    buckets = {
        ("on", T0): (HOUR, 1),
        ("on", T0 + HOUR): (HOUR, 1),
        ("on", T0 + 2 * HOUR): (HOUR, 1),
    }
    _, rows, _ = build_payloads(cfg(), buckets, T0, T0 + 3 * HOUR, {})[COUNT_ON]
    assert [row["sum"] for row in rows] == [1, 2, 3]


def test_base_sums_continue_the_running_total():
    payloads = build_payloads(
        cfg(), {("on", T0): (HOUR, 1)}, T0, T0 + HOUR, {DURATION_ON: 500.0}
    )
    _, rows, _ = payloads[DURATION_ON]
    assert rows[0]["sum"] == 500.0 + 1.0


def test_sums_never_decrease():
    buckets = {
        ("on", T0): (HOUR, 1),
        ("off", T0 + HOUR): (HOUR, 1),
        ("on", T0 + 2 * HOUR): (HOUR, 1),
    }
    payloads = build_payloads(cfg(), buckets, T0, T0 + 3 * HOUR, {})
    for payload in payloads.values():
        sums = [row["sum"] for row in payload.rows]
        assert sums == sorted(sums)


def test_name_defaults_to_entity_id_when_not_configured():
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    metadata, _, _ = payloads[DURATION_ON]
    assert "binary_sensor.grid_status" in metadata["name"]


def test_configured_name_is_used():
    payloads = build_payloads(
        cfg(name="Grid Status"), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {}
    )
    metadata, _, _ = payloads[DURATION_ON]
    assert metadata["name"] == "Grid Status: on (h)"


def test_start_times_are_utc_aware():
    payloads = build_payloads(cfg(), {("on", T0): (HOUR, 0)}, T0, T0 + HOUR, {})
    _, rows, _ = payloads[DURATION_ON]
    assert rows[0]["start"].tzinfo is not None


def test_an_hour_in_one_state_writes_one_duration_row_and_no_count_row():
    # A whole hour on, carried in: seconds but no transition.
    buckets = {("on", T0): (3600.0, 0)}
    payloads = build_payloads(cfg(), buckets, T0, T0 + HOUR, {})

    _, on, _ = payloads[DURATION_ON]
    assert [(r["start"].timestamp(), r["sum"]) for r in on] == [(T0, 1.0)]
    assert payloads[COUNT_ON][1] == []
    assert set(on[0]) == {"start", "sum"}


def test_a_transition_writes_a_count_row_for_the_state_entered_and_durations_for_both():
    # Off for the first half hour, then on.
    buckets = {("off", T0): (1800.0, 0), ("on", T0): (1800.0, 1)}
    payloads = build_payloads(cfg(), buckets, T0, T0 + HOUR, {})

    assert [r["sum"] for r in payloads[DURATION_OFF][1]] == [0.5]
    assert [r["sum"] for r in payloads[DURATION_ON][1]] == [0.5]
    assert [r["sum"] for r in payloads[COUNT_ON][1]] == [1]
    assert payloads[COUNT_OFF][1] == []


def test_a_quiet_hour_writes_nothing_for_an_absent_state_but_the_sum_still_carries():
    # On in hour 0 and hour 2, absent in hour 1: two rows, the second
    # continuing from the first.
    buckets = {("on", T0): (3600.0, 0), ("on", T0 + 2 * HOUR): (1800.0, 1)}
    _, rows, _ = build_payloads(cfg(), buckets, T0, T0 + 3 * HOUR, {})[DURATION_ON]

    assert [(r["start"].timestamp(), r["sum"]) for r in rows] == [
        (T0, 1.0),
        (T0 + 2 * HOUR, 1.5),
    ]


def test_a_standing_row_is_rewritten_even_when_the_state_is_absent():
    # Hour 1 once held a row for "on"; a recompile finds "on" absent there.
    # Without the rewrite the old row's higher sum would stand ahead of
    # every later one.
    buckets = {("on", T0): (3600.0, 0), ("off", T0 + HOUR): (3600.0, 1)}
    _, rows, _ = build_payloads(
        cfg(),
        buckets,
        T0,
        T0 + 2 * HOUR,
        {},
        standing={DURATION_ON: {T0 + HOUR: 99.0}},
    )[DURATION_ON]

    assert [(r["start"].timestamp(), r["sum"]) for r in rows] == [
        (T0, 1.0),
        (T0 + HOUR, 1.0),
    ]


@pytest.mark.parametrize(
    ("stands", "expected"),
    [
        # Outside the window: no row of its own, and none conjured for the
        # hour it names.
        (T0 + 5 * HOUR, [(T0, 1.0)]),
        # Inside it: the quiet hour is rewritten with the carried sum.
        (T0 + HOUR, [(T0, 1.0), (T0 + HOUR, 1.0)]),
    ],
)
def test_a_standing_row_is_rewritten_only_inside_the_window(stands, expected):
    buckets = {("on", T0): (3600.0, 0)}
    _, rows, _ = build_payloads(
        cfg(), buckets, T0, T0 + 2 * HOUR, {}, standing={DURATION_ON: {stands: 99.0}}
    )[DURATION_ON]
    assert [(r["start"].timestamp(), r["sum"]) for r in rows] == expected


def test_a_standing_row_holding_the_same_sum_is_not_written_again():
    # Both hours already hold exactly what this compile computes, so the
    # recorder would rewrite each with itself: two statements for nothing.
    buckets = {("on", T0): (3600.0, 0), ("on", T0 + HOUR): (1800.0, 0)}
    _, rows, _ = build_payloads(
        cfg(),
        buckets,
        T0,
        T0 + 2 * HOUR,
        {},
        standing={DURATION_ON: {T0: 1.0, T0 + HOUR: 1.5}},
    )[DURATION_ON]

    assert rows == []


def test_a_standing_row_holding_a_different_sum_is_written_again():
    # The first hour agrees, the second does not: only the second is
    # rewritten, and with the sum this compile reached.
    buckets = {("on", T0): (3600.0, 0), ("on", T0 + HOUR): (1800.0, 0)}
    _, rows, _ = build_payloads(
        cfg(),
        buckets,
        T0,
        T0 + 2 * HOUR,
        {},
        standing={DURATION_ON: {T0: 1.0, T0 + HOUR: 1.25}},
    )[DURATION_ON]

    assert [(r["start"].timestamp(), r["sum"]) for r in rows] == [(T0 + HOUR, 1.5)]


def test_metadata_declares_a_sum_and_no_mean():
    payloads = build_payloads(cfg(), {("on", T0): (3600.0, 0)}, T0, T0 + HOUR, {})
    metadata, _, _ = payloads[DURATION_ON]
    assert metadata["has_sum"] is True
    assert metadata["has_mean"] is False
    assert metadata["mean_type"] is StatisticMeanType.NONE


def test_a_statistic_with_nothing_to_write_still_carries_its_metadata():
    # Known only from `existing`, absent from the window: no rows, but the
    # metadata - and so the name - is still returned for the import.
    payloads = build_payloads(
        cfg(name="Grid"),
        {("on", T0): (3600.0, 0)},
        T0,
        T0 + HOUR,
        {DURATION_OFF: 4.0},
        existing={DURATION_OFF: "Old: off (h)"},
    )
    metadata, rows, _ = payloads[DURATION_OFF]
    assert rows == []
    assert metadata["name"] == "Grid: off (h)"


DURATION_HEATCOOL = "discrete_statistics:binary_sensor_grid_status_heatcool_duration"
COUNT_HEATCOOL = "discrete_statistics:binary_sensor_grid_status_heatcool_count"


def test_states_sharing_a_token_merge_rather_than_overwrite():
    """`heat_cool` and `heatcool` are one statistic, and must add.

    Keying payloads by raw state would let the second state replace the
    first outright - same base sum, its own values only - and the hour
    would stop totalling wall-clock time. Merging is the deliberate
    behaviour; losing half the hour is not.
    """
    buckets = {
        ("heat_cool", T0): (HOUR * 0.4, 1),
        ("heatcool", T0): (HOUR * 0.6, 2),
    }
    payloads = build_payloads(cfg(), buckets, T0, T0 + HOUR, {})

    assert [k for k in payloads if k.endswith("_duration")] == [DURATION_HEATCOOL]
    _, duration_rows, _ = payloads[DURATION_HEATCOOL]
    _, count_rows, _ = payloads[COUNT_HEATCOOL]
    assert duration_rows[0]["sum"] == pytest.approx(1.0)
    assert count_rows[0]["sum"] == 3


def test_a_rename_reaches_a_state_absent_from_the_window():
    """The display half is swapped; the readable state survives.

    A state the entity has not been in for months would otherwise keep the
    old name forever, because its statistic is only carried, never rebuilt
    from a bucket.
    """
    payloads = build_payloads(
        cfg("Grid Status"),
        {},
        T0,
        T0 + HOUR,
        {},
        {DURATION_ON: "Old Name: on (h)"},
    )
    metadata, rows, _ = payloads[DURATION_ON]
    assert metadata["name"] == "Grid Status: on (h)"
    assert rows == []


def test_a_rename_survives_a_colon_in_the_old_display_name():
    """Split on the last separator, not the first."""
    payloads = build_payloads(
        cfg("Grid"),
        {},
        T0,
        T0 + HOUR,
        {},
        {DURATION_ON: "Outbuilding: Grid Status: on (h)"},
    )
    metadata, _, _ = payloads[DURATION_ON]
    assert metadata["name"] == "Grid: on (h)"


def test_an_unrecognisable_name_is_left_alone_rather_than_mangled():
    payloads = build_payloads(
        cfg("Grid"), {}, T0, T0 + HOUR, {}, {DURATION_ON: "renamed by hand"}
    )
    metadata, _, _ = payloads[DURATION_ON]
    assert metadata["name"] == "renamed by hand"


def test_a_statistic_known_only_from_existing_is_carried_at_its_base():
    """A base sum from `existing` still carries once the state recurs.

    "off" is quiet in the first hour and has no row there, but its second
    hour's row must continue from the stored base rather than from zero.
    """
    payloads = build_payloads(
        cfg(),
        {("on", T0): (HOUR, 1), ("off", T0 + HOUR): (HOUR, 1)},
        T0,
        T0 + 2 * HOUR,
        {DURATION_OFF: 7.5},
        {DURATION_OFF: "x: off (h)"},
    )
    _, off_rows, _ = payloads[DURATION_OFF]
    assert [row["sum"] for row in off_rows] == [8.5]


def test_a_colon_in_the_state_cannot_break_a_later_rename():
    """`rename` splits on the last ": ", so the state must not contain one.

    A text sensor can report anything, and a translated state is an
    arbitrary string too. If the state kept its colon, the next rename of a
    statistic absent from the window would take the boundary from inside the
    state and truncate it.
    """
    buckets = {("Error: pump", T0): (HOUR, 1)}
    payloads = build_payloads(cfg("Grid"), buckets, T0, T0 + HOUR, {})
    statistic_id = "discrete_statistics:binary_sensor_grid_status_errorpump_duration"
    metadata, _, _ = payloads[statistic_id]

    assert metadata["name"] == "Grid: Error pump (h)"
    assert rename(metadata["name"], "Mains") == "Mains: Error pump (h)"


def test_a_colon_in_the_display_name_is_still_fine():
    """Only the state has to be clean; the display may hold any number."""
    payloads = build_payloads(
        cfg("Shed: Grid"), {("on", T0): (HOUR, 1)}, T0, T0 + HOUR, {}
    )
    metadata, _, _ = payloads[DURATION_ON]
    assert metadata["name"] == "Shed: Grid: on (h)"
    assert rename(metadata["name"], "Mains") == "Mains: on (h)"


def test_readable_state_is_verified_against_the_token():
    """Trusting the name blindly would invent a state and split the series.

    Whatever sits after the last ": " becomes the bucket key, and a wrong
    one builds a different statistic ID. So the recovered text has to
    tokenise back to the token the ID actually carries.
    """
    assert readable_state("Grid: heat_cool (h)", "heatcool") == "heat_cool"
    # A display name may hold colons of its own; the state is the last part.
    assert readable_state("Shed: Grid: heat_cool (h)", "heatcool") == "heat_cool"
    # Renamed by hand, or written by an older format: no shape to read.
    assert readable_state("renamed by hand", "heatcool") == "heatcool"
    # Right shape, wrong state - the name does not belong to this ID.
    assert readable_state("Grid: off (h)", "heatcool") == "heatcool"
    assert readable_state("", "heatcool") == "heatcool"
