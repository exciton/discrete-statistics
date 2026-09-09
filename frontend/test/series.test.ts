import { describe, expect, it } from "vitest";
import {
  buildSeries,
  earliestStart,
  percentAxisMax,
  unitLabel,
  valueOf,
} from "../src/series";
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

  it("gives every series every bucket, null where it has no value", () => {
    // ECharts stacks series on a time axis by data index, not by bucket,
    // so a series missing a bucket would stack its later bars on the
    // wrong base. A missing row and a row without a change both become
    // a null point in the bucket's place.
    const sparse: Statistics = {
      "discrete_statistics:climate_zone_heat_duration": [row(0, 24, null), row(24, 24, 3)],
      "discrete_statistics:climate_zone_off_duration": [row(24, 24, 21), row(48, 24, 24)],
    };
    const { series } = buildSeries("climate.zone", stats, sparse, "h", colors);
    expect(series[0].data).toEqual([
      [0, null, 0, 24 * H],
      [24 * H, 3, 24 * H, 48 * H],
      [48 * H, null, 48 * H, 72 * H],
    ]);
    expect(series[1].data).toEqual([
      [0, null, 0, 24 * H],
      [24 * H, 21, 24 * H, 48 * H],
      [48 * H, 24, 48 * H, 72 * H],
    ]);
  });

  it("gives a statistic without rows the same buckets, all null", () => {
    const sparse: Statistics = {
      "discrete_statistics:climate_zone_heat_duration": [row(0, 24, 3)],
    };
    const { series } = buildSeries("climate.zone", stats, sparse, "h", colors);
    expect(series).toHaveLength(2);
    expect(series[1].data).toEqual([[0, null, 0, 24 * H]]);
  });

  it("draws bars side by side when not stacked", () => {
    const { series } = buildSeries("climate.zone", stats, data, "h", colors, "bar");
    expect(series[0].type).toBe("bar");
    expect(series[0].stack).toBeUndefined();
    expect(series[0].stackStrategy).toBeUndefined();
    expect(series[0].data).toHaveLength(2);
  });

  it("draws a line through the bucket starts and closes it at the last end", () => {
    const { series } = buildSeries("climate.zone", stats, data, "h", colors, "line");
    expect(series[0]).toMatchObject({
      type: "line",
      color: "#111111",
      lineStyle: { width: 1.5 },
      smooth: 0.4,
      symbol: "none",
    });
    expect(series[0].stack).toBeUndefined();
    expect(series[0].areaStyle).toBeUndefined();
    expect(series[0].itemStyle).toBeUndefined();
    expect(series[0].data).toEqual([
      [0, 6, 0, 24 * H],
      [24 * H, 12, 24 * H, 48 * H],
      [48 * H, 12, 24 * H, 48 * H],
    ]);
  });

  it("stacks lines with a translucent area", () => {
    const { series } = buildSeries("climate.zone", stats, data, "h", colors, "line-stack");
    expect(series[1]).toMatchObject({
      type: "line",
      stack: "climate.zone",
      stackStrategy: "samesign",
      areaStyle: { color: "#2222223F" },
    });
  });

  it("stacks bars by default", () => {
    const { series } = buildSeries("climate.zone", stats, data, "h", colors);
    expect(series[0]).toMatchObject({ type: "bar", stack: "climate.zone" });
  });

  it("draws a statistic with its own colour, the rest from the palette in order", () => {
    const coloured = [{ ...stats[0], color: "#ff0000" }, stats[1]];
    const { series, legend } = buildSeries("climate.zone", coloured, data, "h", colors);
    expect(series[0].color).toBe("#ff00007F");
    expect(series[1].color).toBe("#2222227F");
    expect(legend[0].itemStyle.color).toBe("#ff0000");
  });

  it("wraps the palette", () => {
    const { series } = buildSeries("climate.zone", stats, data, "h", ["#abcdef"]);
    expect(series[1].color).toBe("#abcdef7F");
  });
});

describe("percentAxisMax", () => {
  it("caps a full stack at 100 whatever rounding left it", () => {
    expect(percentAxisMax({ min: 0, max: 100 })).toBe(100);
    expect(percentAxisMax({ min: 0, max: 100.00000001 })).toBe(100);
  });

  it("rounds the tallest visible stack up at its own order of magnitude", () => {
    expect(percentAxisMax({ min: 0, max: 37 })).toBe(40);
    expect(percentAxisMax({ min: 0, max: 3.2 })).toBe(4);
    expect(percentAxisMax({ min: 0, max: 0.32 })).toBe(0.4);
    expect(percentAxisMax({ min: 0, max: 0.0032 })).toBe(0.004);
    expect(percentAxisMax({ min: 0, max: 0.3 })).toBe(0.3);
    expect(percentAxisMax({ min: 0, max: 0 })).toBe(1);
  });
});

describe("earliestStart", () => {
  const seriesOf = (starts: number[]) =>
    ({ data: starts.map((s) => [s, 1, s, s + H]) }) as never;

  it("ignores buckets with no value", () => {
    expect(
      earliestStart([{ data: [[0, null, 0, H], [H, 1, H, 2 * H]] } as never])
    ).toBe(H);
  });

  it("is the earliest bucket start across every series", () => {
    // The recorder snaps the query outward, so the first bucket can begin
    // before the range the card asked for.
    expect(earliestStart([seriesOf([48 * H, 72 * H]), seriesOf([24 * H])])).toBe(
      24 * H
    );
  });

  it("is undefined when nothing is drawn", () => {
    expect(earliestStart([])).toBeUndefined();
    expect(earliestStart([seriesOf([])])).toBeUndefined();
  });
});
