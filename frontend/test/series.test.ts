import { describe, expect, it } from "vitest";
import { buildSeries, unitLabel, valueOf } from "../src/series";
import type { StateStatistic } from "../src/statistic-ids";
import type { Statistics } from "../src/types";

const H = 3600 * 1000;
const row = (startH: number, hours: number, change: number | null) => ({
  start: startH * H,
  end: (startH + hours) * H,
  change,
});

describe("valueOf", () => {
  it("converts hours of duration to the chosen unit", () => {
    expect(valueOf(row(0, 24, 6), "h")).toBe(6);
    expect(valueOf(row(0, 24, 6), "d")).toBe(0.25);
    expect(valueOf(row(0, 24, 6), "percent")).toBe(25);
  });

  it("percent divides by the row's own length, not a nominal one", () => {
    expect(valueOf(row(0, 23, 11.5), "percent")).toBe(50);
    expect(valueOf(row(0, 25, 12.5), "percent")).toBe(50);
  });

  it("counts pass through, and a missing change is null", () => {
    expect(valueOf(row(0, 24, 7), "count")).toBe(7);
    expect(valueOf(row(0, 24, null), "h")).toBeNull();
    expect(valueOf({ start: 0, end: 24 * H }, "count")).toBeNull();
  });
});

describe("unitLabel", () => {
  it("names the axis", () => {
    expect(unitLabel("h")).toBe("h");
    expect(unitLabel("d")).toBe("d");
    expect(unitLabel("percent")).toBe("%");
    expect(unitLabel("count")).toBe("");
  });
});

describe("buildSeries", () => {
  const stats: StateStatistic[] = [
    { statisticId: "discrete_statistics:climate_zone_heat_duration", token: "heat", label: "Heating", metric: "duration" },
    { statisticId: "discrete_statistics:climate_zone_off_duration", token: "off", label: "Off", metric: "duration" },
  ];
  const data: Statistics = {
    "discrete_statistics:climate_zone_heat_duration": [row(0, 24, 6), row(24, 24, 12)],
    "discrete_statistics:climate_zone_off_duration": [row(0, 24, 18), row(24, 24, 12)],
  };
  const colors = ["#111111", "#222222", "#333333"];

  it("makes one bar series per state, stacked on the entity, in percent", () => {
    const { series, legend } = buildSeries("climate.zone", stats, data, "percent", colors);
    expect(series).toHaveLength(2);
    expect(series[0]).toMatchObject({
      id: "discrete_statistics:climate_zone_heat_duration",
      name: "Heating",
      type: "bar",
      stack: "climate.zone",
      stackStrategy: "samesign",
      color: "#1111117F",
      itemStyle: { borderColor: "#111111", borderWidth: 1.5 },
    });
    expect(series[0].data).toEqual([
      [0, 25, 0, 24 * H],
      [24 * H, 50, 24 * H, 48 * H],
    ]);
    expect(series[1].data.map((d) => d[1])).toEqual([75, 50]);
    expect(legend).toEqual([
      { id: "discrete_statistics:climate_zone_heat_duration", name: "Heating", itemStyle: { color: "#111111" }, noLabelClick: true },
      { id: "discrete_statistics:climate_zone_off_duration", name: "Off", itemStyle: { color: "#222222" }, noLabelClick: true },
    ]);
  });

  it("skips rows without a change and statistics without rows", () => {
    const sparse: Statistics = {
      "discrete_statistics:climate_zone_heat_duration": [row(0, 24, null), row(24, 24, 3)],
    };
    const { series } = buildSeries("climate.zone", stats, sparse, "h", colors);
    expect(series).toHaveLength(2);
    expect(series[0].data).toEqual([[24 * H, 3, 24 * H, 48 * H]]);
    expect(series[1].data).toEqual([]);
  });

  it("wraps the palette", () => {
    const { series } = buildSeries("climate.zone", stats, data, "h", ["#abcdef"]);
    expect(series[1].color).toBe("#abcdef7F");
  });
});
