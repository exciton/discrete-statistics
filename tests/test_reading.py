"""One sensor's value from the sums at hour edges, part hours and the live tail."""

from datetime import UTC, datetime

import pytest

from custom_components.discrete_statistics.compiler import Timeline
from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import HOUR
from custom_components.discrete_statistics.reading import (
    REASON_NOT_RECORDED,
    Custom,
    Frame,
    Partial,
    PartialValue,
    Pieces,
    Spec,
    compute,
    edges_of,
    exact_partial,
    pieces,
    plan,
    prorate,
    spec_from,
    suggested_entity_id,
)

ENTITY = "binary_sensor.grid_status"
ON_D = "discrete_statistics:binary_sensor_grid_status_on_duration"
ON_C = "discrete_statistics:binary_sensor_grid_status_on_count"
OFF_D = "discrete_statistics:binary_sensor_grid_status_off_duration"
OFF_C = "discrete_statistics:binary_sensor_grid_status_off_count"
EXISTING = {ON_D: "", ON_C: "", OFF_D: "", OFF_C: ""}

T0 = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
W_END = T0 + 3 * HOUR
NOW = T0 + 3 * HOUR + 20 * 60
# Three compiled hours. Off carried in; on at :30 of the first hour, off
# at :30 of the second, on at :30 of the third: on for 1.5 of them, off for
# 1.5, on twice, off once. The sums at each hour's end:
SUMS = {
    T0 + HOUR: {ON_D: 0.5, ON_C: 1.0, OFF_D: 0.5, OFF_C: 0.0},
    T0 + 2 * HOUR: {ON_D: 1.0, ON_C: 1.0, OFF_D: 1.0, OFF_C: 1.0},
    W_END: {ON_D: 1.5, ON_C: 2.0, OFF_D: 1.5, OFF_C: 1.0},
}
# The first hour's own timeline, as the compiler would read it.
HOUR_0 = Timeline(T0, "off", [(T0 + 1800, "on")])
# Then on from the watermark, off ten minutes later.
TAIL = Timeline(W_END, "on", [(W_END + 600, "off")])
FRAME = Frame(EXISTING, W_END, T0)


def sum_at(statistic_id, edge):
    """Every series starts at T0 and is flat before it; only hour edges exist."""
    return 0.0 if edge <= T0 else SUMS[edge][statistic_id]


def no_partial(partial):
    raise AssertionError(f"no part hour expected, got {partial}")


def cfg(states=None):
    return EntityConfig(
        entity_id=ENTITY, name=None, default="record_known", states=states or {}
    )


def spec(states=("on",), metric="duration", period="today", live=True, custom=None):
    return Spec(tuple(states), metric, period, live, custom)


def value(spec_, frame=FRAME, timeline=TAIL, now=NOW, config=None):
    return compute(
        config or cfg(), spec_, frame, sum_at, no_partial, timeline, now, UTC
    ).value


# A custom window: what the coordinator hands `compute` after rendering.
CUSTOM = Custom("{{ x }}", None, None)


def custom(window, **kwargs):
    kwargs.setdefault("custom", CUSTOM)
    return spec(period="custom", **kwargs), window


def read(spec_, window, partial_at, frame=FRAME, timeline=TAIL, now=NOW):
    return compute(cfg(), spec_, frame, sum_at, partial_at, timeline, now, UTC, window)


def test_duration_is_the_compiled_change_plus_the_live_tail():
    assert value(spec()) == 1.67


def test_a_sensor_that_is_not_live_stops_at_the_watermark():
    assert value(spec(live=False)) == 1.5


def test_the_tail_counts_time_in_the_other_state_too():
    assert value(spec(states=("off",))) == 1.67


def test_count_is_the_compiled_count_plus_the_tail_transitions():
    assert value(spec(metric="count")) == 2
    assert value(spec(states=("off",), metric="count")) == 2
    assert isinstance(value(spec(metric="count")), int)


def test_share_divides_by_the_time_read():
    # 1.6667 h on out of 3 h 20 min read.
    assert value(spec(metric="share")) == 50.0


def test_a_set_of_states_is_summed():
    assert value(spec(states=("on", "off"))) == 3.33
    assert value(spec(states=(), metric="count")) == 4


def test_a_finished_period_reads_only_the_statistics():
    assert value(spec(period="yesterday")) == 0.0
    reading = compute(
        cfg(), spec(period="yesterday"), FRAME, sum_at, no_partial, TAIL, NOW, UTC
    )
    assert reading.period_start == T0 - 24 * HOUR
    assert reading.period_end == T0


def test_all_time_starts_where_the_series_does():
    frame = Frame(EXISTING, W_END, T0 - HOUR)
    assert value(spec(period="all_time", metric="share"), frame=frame) == 38.5
    reading = compute(
        cfg(), spec(period="all_time"), frame, sum_at, no_partial, TAIL, NOW, UTC
    )
    assert reading.period_start == T0 - HOUR
    assert reading.period_end is None


def test_nothing_compiled_yet_has_no_value_and_no_reason():
    reading = compute(
        cfg(), spec(), Frame({}, None, None), sum_at, no_partial, None, NOW, UTC
    )
    assert reading == (None, T0, T0 + 24 * HOUR, None, False)


def test_a_state_the_entry_ignores_and_never_recorded_is_refused():
    reading = compute(
        cfg(), spec(states=("unknown",)), FRAME, sum_at, no_partial, TAIL, NOW, UTC
    )
    assert reading.value is None
    assert reading.reason == REASON_NOT_RECORDED
    # But a state the entry now ignores that has a statistic is still read;
    # the tail is whatever the compiler's timeline says, as always.
    assert value(spec(), config=cfg(states={"on": "ignore"})) == 1.67


def test_a_period_that_starts_after_the_watermark_is_all_tail():
    frame = Frame(EXISTING, T0, T0 - 24 * HOUR)
    timeline = Timeline(T0, "on", [(T0 + 30 * 60, "off")])
    assert value(spec(), frame=frame, timeline=timeline, now=T0 + HOUR) == 0.5


def test_no_timeline_means_no_tail():
    assert value(spec(), timeline=None) == 1.5


def test_edges_are_the_period_start_and_the_end_of_the_compiled_part():
    assert edges_of(plan(spec(), FRAME, NOW, UTC)) == {T0, W_END}
    assert edges_of(plan(spec(period="yesterday"), FRAME, NOW, UTC)) == {
        T0 - 24 * HOUR,
        T0,
    }
    assert plan(spec(), Frame({}, None, None), NOW, UTC) is None
    assert plan(spec(period="all_time"), Frame(EXISTING, W_END, None), NOW, UTC) is None


def test_spec_from_fills_the_defaults():
    assert spec_from({}) == Spec((), "duration", "this_month", True)
    assert spec_from(
        {"states": ["on"], "metric": "count", "period": "today", "live": False}
    ) == Spec(("on",), "count", "today", False)


def test_suggested_entity_id_names_the_statistic_it_reads():
    assert (
        suggested_entity_id(ENTITY, spec())
        == "sensor.discrete_binary_sensor_grid_status_on_duration_today"
    )
    assert (
        suggested_entity_id(
            ENTITY, spec(states=("heat_cool", "off"), period="all_time")
        )
        == "sensor.discrete_binary_sensor_grid_status_heatcool_off_duration_all_time"
    )
    assert (
        suggested_entity_id(ENTITY, spec(states=(), metric="count"))
        == "sensor.discrete_binary_sensor_grid_status_all_count_today"
    )


# --- pieces ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # Today: whole hours to the watermark, then the tail.
        (T0, T0 + 24 * HOUR, Pieces((T0, W_END), (), (W_END, NOW))),
        # Yesterday: whole hours, finished, no tail.
        (T0 - 24 * HOUR, T0, Pieces((T0 - 24 * HOUR, T0), (), None)),
        # A start a quarter past: the first hour is a part hour.
        (
            T0 + 900,
            T0 + 24 * HOUR,
            Pieces(
                (T0 + HOUR, W_END), (Partial(T0, T0 + 900, T0 + HOUR),), (W_END, NOW)
            ),
        ),
        # An end at half past two, finished: the last hour is a part hour.
        (
            T0,
            T0 + 9000,
            Pieces(
                (T0, T0 + 2 * HOUR),
                (Partial(T0 + 2 * HOUR, T0 + 2 * HOUR, T0 + 9000),),
                None,
            ),
        ),
        # Both edges inside one hour: one part hour and nothing else.
        (T0 + 600, T0 + 1800, Pieces(None, (Partial(T0, T0 + 600, T0 + 1800),), None)),
        # Entirely after the watermark: all tail.
        (W_END + 600, NOW + HOUR, Pieces(None, (), (W_END + 600, NOW))),
        # Entirely in the future: nothing.
        (NOW, NOW + HOUR, Pieces(None, (), None)),
        # The last hour at twenty past three: a part hour, then the tail.
        (
            NOW - HOUR,
            NOW,
            Pieces(None, (Partial(T0 + 2 * HOUR, NOW - HOUR, W_END),), (W_END, NOW)),
        ),
    ],
)
def test_pieces(start, end, expected):
    assert pieces(start, end, W_END, NOW) == expected


def test_a_part_hours_edges_are_wanted_too():
    spec_, window = custom((T0 + 900, T0 + 24 * HOUR))
    assert edges_of(plan(spec_, FRAME, NOW, UTC, window)) == {T0, T0 + HOUR, W_END}
    assert edges_of(plan(spec(period="last_hour"), FRAME, NOW, UTC)) == {
        T0 + 2 * HOUR,
        W_END,
    }


# --- a part hour's value ---------------------------------------------------


def test_an_exact_part_hour_is_the_timeline_cut_at_the_edge():
    assert exact_partial(Partial(T0, T0 + 900, T0 + HOUR), HOUR_0) == PartialValue(
        {"off": 900.0, "on": 1800.0}, {"off": 0, "on": 1}, True
    )


def test_a_pro_rated_part_hour_scales_the_hours_change():
    change_seconds = {"on": 1800.0, "off": 1800.0}
    change_counts = {"on": 1.0, "off": 0.0}
    assert prorate(Partial(T0, T0 + 900, T0 + HOUR), change_seconds, change_counts) == (
        PartialValue({"on": 1350.0, "off": 1350.0}, {"on": 1, "off": 0}, False)
    )


@pytest.mark.parametrize(
    ("start", "count"),
    # One change in the hour: a whole one when at least half the hour is
    # inside the window, none otherwise.
    [(T0 + 900, 1), (T0 + 1800, 1), (T0 + 2700, 0)],
)
def test_a_pro_rated_count_is_rounded_half_up(start, count):
    partial = Partial(T0, start, T0 + HOUR)
    assert prorate(partial, {"on": 1800.0}, {"on": 1.0}).counts == {"on": count}


# --- compute with part hours ----------------------------------------------


def exact_hour_0(partial):
    assert partial.hour == T0
    return exact_partial(partial, HOUR_0)


def prorated_hour_0(partial):
    assert partial.hour == T0
    return prorate(partial, {"on": 1800.0, "off": 1800.0}, {"on": 1.0, "off": 0.0})


def test_an_exact_part_hour_adds_to_the_whole_ones_and_the_tail():
    spec_, window = custom((T0 + 900, T0 + 24 * HOUR))
    # Half an hour on in the part hour, one whole hour on after it, ten
    # minutes on in the tail.
    reading = read(spec_, window, exact_hour_0)
    assert reading.value == 1.67
    assert reading.estimated is False
    assert reading.period_start == T0 + 900
    spec_, window = custom((T0 + 900, T0 + 24 * HOUR), metric="count")
    assert read(spec_, window, exact_hour_0).value == 2


def test_a_pro_rated_part_hour_is_marked_estimated():
    spec_, window = custom((T0 + 900, T0 + 24 * HOUR))
    # Three quarters of the hour's half hour on, then the same as above.
    reading = read(spec_, window, prorated_hour_0)
    assert reading.value == 1.54
    assert reading.estimated is True
    spec_, window = custom((T0 + 2700, T0 + 24 * HOUR))
    assert read(spec_, window, prorated_hour_0).value == 1.29
    spec_, window = custom((T0 + 900, T0 + 24 * HOUR), metric="count")
    assert read(spec_, window, prorated_hour_0).value == 2
    spec_, window = custom((T0 + 2700, T0 + 24 * HOUR), metric="count")
    assert read(spec_, window, prorated_hour_0).value == 1


def test_a_part_hour_at_the_end_of_a_finished_window():
    spec_, window = custom((T0, T0 + 9000))

    def prorated_hour_2(partial):
        assert partial == Partial(T0 + 2 * HOUR, T0 + 2 * HOUR, T0 + 9000)
        return prorate(partial, {"on": 1800.0, "off": 1800.0}, {"on": 1.0, "off": 0.0})

    # Two whole hours: one hour on; then half of the third hour's half hour.
    reading = read(spec_, window, prorated_hour_2)
    assert reading.value == 1.25
    assert reading.estimated is True
    assert reading.period_end == T0 + 9000


def test_a_part_hour_nobody_can_answer_contributes_nothing():
    spec_, window = custom((T0 + 900, T0 + 24 * HOUR))
    reading = read(spec_, window, lambda partial: None)
    assert reading.value == 1.17
    assert reading.estimated is False


def test_a_rolling_period_is_read_like_any_other():
    # The last hour at twenty past three: forty minutes of the third hour,
    # exactly, then the tail.
    def exact_hour_2(partial):
        assert partial == Partial(T0 + 2 * HOUR, NOW - HOUR, W_END)
        return exact_partial(
            partial, Timeline(T0 + 2 * HOUR, "off", [(T0 + 9000, "on")])
        )

    reading = compute(
        cfg(), spec(period="last_hour"), FRAME, sum_at, exact_hour_2, TAIL, NOW, UTC
    )
    assert reading.value == 0.67
    assert reading.period_start == NOW - HOUR
    assert reading.period_end == NOW


def test_a_custom_period_needs_its_window():
    spec_, _ = custom(None)
    with pytest.raises(ValueError):
        compute(cfg(), spec_, FRAME, sum_at, no_partial, TAIL, NOW, UTC)


def test_spec_from_reads_the_custom_templates_only_for_a_custom_period():
    assert spec_from(
        {"period": "custom", "start": "{{ x }}", "end": None, "duration": 3600.0}
    ).custom == Custom("{{ x }}", None, 3600.0)
    assert spec_from({"period": "today", "start": "{{ x }}"}).custom is None
