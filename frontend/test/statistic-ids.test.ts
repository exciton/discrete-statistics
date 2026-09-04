import { describe, expect, it } from "vitest";
import {
  entitySlug,
  parseStatisticId,
  stateLabel,
  stateToken,
  statisticsForEntity,
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
  });
});
