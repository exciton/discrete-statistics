import { describe, expect, it } from "vitest";
import {
  entityLabel,
  entitySlug,
  parseStatisticId,
  resolveSeries,
  stateLabel,
  stateToken,
  statisticsForEntity,
  statisticsForRows,
  entitiesWithStatistics,
  stateOptionsFor,
} from "../src/statistic-ids";
import type { StatisticsMetaData } from "../src/types";

const meta = (id: string, name: string): StatisticsMetaData => ({
  statistic_id: id,
  source: "discrete_statistics",
  name,
  statistics_unit_of_measurement: id.endsWith("duration") ? "h" : null,
  has_sum: true,
  unit_class: id.endsWith("duration") ? "duration" : null,
});

describe("parseStatisticId", () => {
  it("reads entity, state and metric from the right", () => {
    expect(parseStatisticId("discrete_statistics:climate_zone_heatcool_count")).toEqual({
      entitySlug: "climate_zone",
      token: "heatcool",
      metric: "count",
    });
    expect(parseStatisticId("discrete_statistics:climate_zone_heat_cool_count")).toEqual({
      entitySlug: "climate_zone_heat",
      token: "cool",
      metric: "count",
    });
  });

  it("rejects IDs that are not ours", () => {
    expect(parseStatisticId("sensor.temperature")).toBeNull();
    expect(parseStatisticId("other:climate_zone_heat_count")).toBeNull();
    expect(parseStatisticId("discrete_statistics:climate_zone_heat_seconds")).toBeNull();
    expect(parseStatisticId("discrete_statistics:tooshort")).toBeNull();
  });
});

describe("tokens", () => {
  it("slugifies the entity with underscores and the state with none", () => {
    expect(entitySlug("binary_sensor.grid_status")).toBe("binary_sensor_grid_status");
    expect(stateToken("heat_cool")).toBe("heatcool");
    expect(stateToken("Heat Cool")).toBe("heatcool");
    expect(stateToken("heatcool")).toBe("heatcool");
  });
});

describe("stateLabel", () => {
  it("takes the state from the stored name, without the metric suffix", () => {
    const m = meta("discrete_statistics:climate_zone_heatcool_duration", "Zone: heat_cool (h)");
    expect(stateLabel(m, "heatcool")).toBe("heat_cool");
    const c = meta("discrete_statistics:climate_zone_heatcool_count", "Zone: heat_cool (#)");
    expect(stateLabel(c, "heatcool")).toBe("heat_cool");
  });

  it("survives a display name with colons", () => {
    const m = meta("discrete_statistics:sensor_x_on_duration", "A: B: On (h)");
    expect(stateLabel(m, "on")).toBe("On");
  });

  it("falls back to the token when there is no name of that shape", () => {
    expect(stateLabel(undefined, "on")).toBe("on");
    const odd = meta("discrete_statistics:sensor_x_on_duration", "renamed by hand");
    expect(stateLabel(odd, "on")).toBe("on");
  });
});

describe("statisticsForEntity", () => {
  const all = [
    meta("discrete_statistics:climate_zone_heat_duration", "Zone: heat (h)"),
    meta("discrete_statistics:climate_zone_heat_count", "Zone: heat (#)"),
    meta("discrete_statistics:climate_zone_cool_duration", "Zone: cool (h)"),
    meta("discrete_statistics:climate_zone_heatcool_duration", "Zone: heat_cool (h)"),
    meta("discrete_statistics:climate_zone_heat_cool_duration", "Zone heat: cool (h)"),
    meta("discrete_statistics:sensor_other_on_duration", "Other: on (h)"),
  ];

  it("returns the entity's statistics for one metric, nesting entities apart", () => {
    const got = statisticsForEntity("climate.zone", "duration", all);
    expect(got.map((s) => s.token)).toEqual(["heat", "cool", "heatcool"]);
    expect(got[0]).toEqual({
      statisticId: "discrete_statistics:climate_zone_heat_duration",
      token: "heat",
      label: "heat",
      entityLabel: "Zone",
      metric: "duration",
    });
    expect(statisticsForEntity("climate.zone_heat", "duration", all).map((s) => s.token)).toEqual(["cool"]);
  });

  it("narrows to the listed states, matched by token, in the listed order", () => {
    const got = statisticsForEntity("climate.zone", "duration", all, { states: ["heat_cool", "heat"] });
    expect(got.map((s) => s.token)).toEqual(["heatcool", "heat"]);
  });

  it("drops ignored states, matched by token, and sorts the rest by label", () => {
    const got = statisticsForEntity("climate.zone", "duration", all, { ignore_states: ["Heat"] });
    expect(got.map((s) => s.token)).toEqual(["cool", "heatcool"]);
  });

  it("with both, the include-list leads and unlisted states follow alphabetically", () => {
    const got = statisticsForEntity("climate.zone", "duration", all, {
      states: ["heat_cool"],
      ignore_states: ["off"],
    });
    expect(got.map((s) => s.token)).toEqual(["heatcool", "cool", "heat"]);
  });

  it("carries a listed state's name and colour, and an entry without them has none", () => {
    const got = statisticsForEntity("climate.zone", "duration", all, {
      states: [{ state: "heat", name: "Heating", color: "red" }, "cool", { state: "heat_cool" }],
    });
    expect(got.map((s) => [s.token, s.label, s.color])).toEqual([
      ["heat", "Heating", "red"],
      ["cool", "cool", undefined],
      ["heatcool", "heat_cool", undefined],
    ]);
  });

  it("the exclude-list vetoes a listed state too", () => {
    const got = statisticsForEntity("climate.zone", "duration", all, {
      states: ["heat_cool", "heat"],
      ignore_states: ["heat"],
    });
    expect(got.map((s) => s.token)).toEqual(["heatcool", "cool"]);
  });

  it("is empty for an entity with nothing recorded", () => {
    expect(statisticsForEntity("sensor.none", "duration", all)).toEqual([]);
  });

  it("matches non-Latin filter entries by label, since they have no token", () => {
    // python-slugify transliterates "打开" to "dakai"; this approximation
    // cannot, which is exactly why statisticsForEntity also compares
    // against the label.
    expect(stateToken("打开")).toBe("");
    const withDoor = [
      ...all,
      meta("discrete_statistics:sensor_door_dakai_duration", "Door: 打开 (h)"),
    ];
    expect(
      statisticsForEntity("sensor.door", "duration", withDoor, { states: ["打开"] }).map((s) => s.token)
    ).toEqual(["dakai"]);
    expect(
      statisticsForEntity("sensor.door", "duration", withDoor, { ignore_states: ["打开"] }).map((s) => s.token)
    ).toEqual([]);
    // A configured name is drawn, not matched on: the stored name still
    // names the state.
    expect(
      statisticsForEntity("sensor.door", "duration", withDoor, {
        states: [{ state: "打开", name: "Open" }],
      }).map((s) => s.label)
    ).toEqual(["Open"]);
  });
});

describe("entitiesWithStatistics", () => {
  const meta = (id: string) =>
    ({ statistic_id: id, source: "discrete_statistics" }) as never;

  it("keeps the entities some statistic belongs to, in the order given", () => {
    const known = [
      "climate.zone",
      "binary_sensor.door",
      "sensor.unrelated",
      "climate.zone_heat",
    ];
    expect(
      entitiesWithStatistics(known, [
        meta("discrete_statistics:climate_zone_heat_duration"),
        meta("discrete_statistics:binary_sensor_door_on_count"),
        meta("sensor:not_ours"),
      ])
    ).toEqual(["climate.zone", "binary_sensor.door"]);
  });
});

const rowsMeta = [
  meta("discrete_statistics:binary_sensor_hall_motion_on_duration", "Hall Motion: Detected (h)"),
  meta("discrete_statistics:binary_sensor_porch_motion_on_duration", "Porch Motion: Detected (h)"),
  meta("discrete_statistics:cover_gate_open_duration", "Gate: Open (h)"),
  meta("discrete_statistics:cover_gate_closed_duration", "Gate: Closed (h)"),
];

describe("entityLabel", () => {
  it("takes the entity's half of the stored name", () => {
    expect(entityLabel(rowsMeta[0], "binary_sensor.hall_motion")).toBe("Hall Motion");
  });

  it("falls back to the entity ID when there is no stored name to split", () => {
    expect(entityLabel(undefined, "cover.gate")).toBe("cover.gate");
    expect(entityLabel(meta("discrete_statistics:cover_gate_open_duration", "Open (h)"), "cover.gate")).toBe("cover.gate");
  });
});

describe("statisticsForRows", () => {
  it("names each series for its entity, in config order", () => {
    const series = statisticsForRows(
      [
        { entity: "cover.gate", state: "open" },
        { entity: "binary_sensor.hall_motion", state: "on" },
      ],
      "duration",
      rowsMeta
    );
    expect(series.map((s) => [s.statisticId, s.label])).toEqual([
      ["discrete_statistics:cover_gate_open_duration", "Gate"],
      ["discrete_statistics:binary_sensor_hall_motion_on_duration", "Hall Motion"],
    ]);
  });

  it("names both series in full when one entity appears twice", () => {
    const series = statisticsForRows(
      [
        { entity: "cover.gate", state: "open" },
        { entity: "cover.gate", state: "closed" },
        { entity: "binary_sensor.hall_motion", state: "on" },
      ],
      "duration",
      rowsMeta
    );
    expect(series.map((s) => s.label)).toEqual(["Gate: Open", "Gate: Closed", "Hall Motion"]);
  });

  it("takes the configured name and colour over the entity's own", () => {
    const [series] = statisticsForRows(
      [{ entity: "cover.gate", state: "open", name: "Gate open", color: "red" }],
      "duration",
      rowsMeta
    );
    expect(series.label).toBe("Gate open");
    expect(series.color).toBe("red");
  });

  it("drops a row with no statistic, and a row that names no entity", () => {
    expect(
      statisticsForRows(
        [
          { entity: "cover.gate", state: "ajar" },
          { entity: "light.nowhere", state: "on" },
          "on",
          { state: "on" },
          { entity: "cover.gate", state: "open" },
        ],
        "duration",
        rowsMeta
      ).map((s) => s.statisticId)
    ).toEqual(["discrete_statistics:cover_gate_open_duration"]);
  });

  it("matches a state whose text has no token by its label", () => {
    // The integration transliterates the state, the card cannot, so the
    // token never agrees and the stored label is the only way in.
    const cn = meta("discrete_statistics:cover_gate_dakai_duration", "Gate: 打开 (h)");
    const [series] = statisticsForRows(
      [{ entity: "cover.gate", state: "打开" }],
      "duration",
      [cn]
    );
    expect(series.statisticId).toBe("discrete_statistics:cover_gate_dakai_duration");
  });
});

describe("duplicate rows", () => {
  const dupeMeta = [
    meta("discrete_statistics:cover_gate_open_duration", "Gate: Open (h)"),
    meta("discrete_statistics:binary_sensor_hall_motion_on_duration", "Hall: On (h)"),
    meta("discrete_statistics:binary_sensor_porch_motion_on_duration", "Porch: On (h)"),
    meta("discrete_statistics:climate_zone_heatcool_duration", "Zone: Heat/Cool (h)"),
  ];

  it("draws two rows on one statistic once, the first winning", () => {
    const series = statisticsForRows(
      [
        { entity: "cover.gate", state: "open", name: "First", color: "red" },
        { entity: "cover.gate", state: "open", name: "Second", color: "blue" },
      ],
      "duration",
      dupeMeta
    );
    expect(series).toHaveLength(1);
    expect(series[0].label).toBe("First");
    expect(series[0].color).toBe("red");
  });

  it("folds two rows whose states tokenise alike", () => {
    const series = statisticsForRows(
      [
        { entity: "climate.zone", state: "heat_cool" },
        { entity: "climate.zone", state: "heatcool" },
      ],
      "duration",
      dupeMeta
    );
    expect(series.map((s) => s.statisticId)).toEqual([
      "discrete_statistics:climate_zone_heatcool_duration",
    ]);
  });

  it("keeps the same state on two different entities", () => {
    const series = statisticsForRows(
      [
        { entity: "binary_sensor.hall_motion", state: "on" },
        { entity: "binary_sensor.porch_motion", state: "on" },
      ],
      "duration",
      dupeMeta
    );
    expect(series.map((s) => s.statisticId)).toEqual([
      "discrete_statistics:binary_sensor_hall_motion_on_duration",
      "discrete_statistics:binary_sensor_porch_motion_on_duration",
    ]);
  });

  it("draws a state listed twice on one entity once", () => {
    const series = statisticsForEntity("cover.gate", "duration", dupeMeta, {
      states: ["open", "open"],
    });
    expect(series.map((s) => s.statisticId)).toEqual([
      "discrete_statistics:cover_gate_open_duration",
    ]);
  });
});

describe("resolveSeries", () => {
  it("takes the entity's states when the card names an entity", () => {
    const series = resolveSeries(
      { entity: "cover.gate", states: ["open"] },
      "duration",
      rowsMeta
    );
    expect(series.map((s) => s.label)).toEqual(["Open"]);
  });

  it("takes the rows' entities when the card names none", () => {
    const series = resolveSeries(
      { states: [{ entity: "cover.gate", state: "open" }] },
      "duration",
      rowsMeta
    );
    expect(series.map((s) => s.label)).toEqual(["Gate"]);
  });
});

describe("stateOptionsFor", () => {
  const optionsMeta = [
    meta("discrete_statistics:cover_gate_open_duration", "Gate: Open (h)"),
    meta("discrete_statistics:cover_gate_closed_duration", "Gate: Closed (h)"),
    meta("discrete_statistics:cover_gate_open_count", "Gate: Open (#)"),
    meta("discrete_statistics:binary_sensor_hall_motion_on_duration", "Hall: On (h)"),
  ];

  it("gives each entity its states in order, by token and label", () => {
    expect(
      stateOptionsFor(["cover.gate", "binary_sensor.hall_motion"], "duration", optionsMeta)
    ).toEqual({
      "cover.gate": [
        { value: "open", label: "Open" },
        { value: "closed", label: "Closed" },
      ],
      "binary_sensor.hall_motion": [{ value: "on", label: "On" }],
    });
  });

  it("leaves out an entity with no statistics", () => {
    expect(
      Object.keys(stateOptionsFor(["light.hall", "cover.gate"], "duration", optionsMeta))
    ).toEqual(["cover.gate"]);
  });

  it("includes only the metric asked for", () => {
    expect(stateOptionsFor(["cover.gate"], "count", optionsMeta)).toEqual({
      "cover.gate": [{ value: "open", label: "Open" }],
    });
  });
});
