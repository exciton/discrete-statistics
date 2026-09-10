"""One sensor's value from the sums at two edges and the live tail."""

from datetime import UTC, datetime

from custom_components.discrete_statistics.compiler import Timeline
from custom_components.discrete_statistics.config import EntityConfig
from custom_components.discrete_statistics.const import HOUR
from custom_components.discrete_statistics.reading import (
    REASON_NOT_RECORDED,
    Frame,
    Spec,
    compute,
    edges,
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
# Three compiled hours: on for 1.5 of them, off for 1.5, on twice, off once.
FINAL = {ON_D: 1.5, ON_C: 2.0, OFF_D: 1.5, OFF_C: 1.0}
# Then on from the watermark, off ten minutes later.
TAIL = Timeline(W_END, "on", [(W_END + 600, "off")])
FRAME = Frame(EXISTING, W_END, T0)


def sum_at(statistic_id, edge):
    """Every series starts at T0 and is flat before it."""
    return 0.0 if edge <= T0 else FINAL[statistic_id]


def cfg(states=None):
    return EntityConfig(
        entity_id=ENTITY, name=None, default="record_known", states=states or {}
    )


def spec(states=("on",), metric="duration", period="today", live=True):
    return Spec(tuple(states), metric, period, live)


def value(spec_, frame=FRAME, timeline=TAIL, now=NOW, config=None):
    return compute(config or cfg(), spec_, frame, sum_at, timeline, now, UTC).value


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
    reading = compute(cfg(), spec(period="yesterday"), FRAME, sum_at, TAIL, NOW, UTC)
    assert reading.period_start == T0 - 24 * HOUR
    assert reading.period_end == T0


def test_all_time_starts_where_the_series_does():
    frame = Frame(EXISTING, W_END, T0 - HOUR)
    assert value(spec(period="all_time", metric="share"), frame=frame) == 38.5
    reading = compute(cfg(), spec(period="all_time"), frame, sum_at, TAIL, NOW, UTC)
    assert reading.period_start == T0 - HOUR
    assert reading.period_end is None


def test_nothing_compiled_yet_has_no_value_and_no_reason():
    reading = compute(cfg(), spec(), Frame({}, None, None), sum_at, None, NOW, UTC)
    assert reading == (None, T0, T0 + 24 * HOUR, None)


def test_a_state_the_entry_ignores_and_never_recorded_is_refused():
    reading = compute(cfg(), spec(states=("unknown",)), FRAME, sum_at, TAIL, NOW, UTC)
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
    assert edges(spec(), FRAME, NOW, UTC) == {T0, W_END}
    assert edges(spec(period="yesterday"), FRAME, NOW, UTC) == {T0 - 24 * HOUR, T0}
    assert edges(spec(), Frame({}, None, None), NOW, UTC) == set()
    assert (
        edges(spec(period="all_time"), Frame(EXISTING, W_END, None), NOW, UTC) == set()
    )


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
