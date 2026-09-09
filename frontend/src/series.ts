import { bucketHours, type ResolvedUnit } from "./period";
import type { StateStatistic } from "./statistic-ids";
import type { ChartType, StatisticValue, Statistics } from "./types";

export interface ChartSeries {
  // The fields of a series ha-chart-base is given; it types them itself,
  // this is only what the card sets.
  id: string;
  name: string;
  type: "bar" | "line";
  stack?: string;
  stackStrategy?: "samesign";
  color: string;
  itemStyle?: { borderColor: string; borderWidth: number };
  lineStyle?: { width: number };
  areaStyle?: { color: string };
  smooth?: number;
  symbol?: "none";
  cursor: "default";
  animationDurationUpdate: 0;
  // [point time, value, bucket start, bucket end], as the stock statistics
  // chart draws bars (statistics-chart-data.ts:198), so a tooltip can
  // name the bucket the point represents. Every series holds every bucket
  // in the same order, null where it has no value: ECharts stacks series
  // on a time axis by data index, not by x value, so a series missing a
  // bucket would stack its later bars on the wrong base.
  data: [number, number | null, number, number][];
}

// The earliest bucket start drawn, or undefined when nothing is drawn.
export function earliestStart(series: ChartSeries[]): number | undefined {
  let earliest: number | undefined;
  for (const s of series) {
    for (const point of s.data) {
      if (point[1] !== null && (earliest === undefined || point[0] < earliest)) {
        earliest = point[0];
      }
    }
  }
  return earliest;
}

export interface LegendItem {
  id: string;
  name: string;
  itemStyle: { color: string };
  noLabelClick: true;
}

export function valueOf(
  row: StatisticValue,
  unit: ResolvedUnit
): number | null {
  const change = row.change;
  if (change === null || change === undefined) {
    return null;
  }
  switch (unit) {
    case "d":
      return change / 24;
    case "percent":
      // The denominator is this row's own length, never a nominal hour
      // count or the sum over states: a 23- or 25-hour bucket (DST) must
      // still read 100% when the entity held one state throughout.
      return (100 * change) / bucketHours(row);
    default:
      return change;
  }
}

// The top of a percent axis: 100 when the visible stack fills the bucket,
// otherwise the tallest visible stack rounded up to its own order of
// magnitude — 37 reads to 40, 3.2 to 4, 0.032 to 0.04 — so the axis
// follows what the legend leaves showing however small that is. Capped
// rather than rounded at the top because a full stack's float sum can
// land a hair over 100, which would push the axis out to 110.
export function percentAxisMax({ max }: { min: number; max: number }): number {
  if (max >= 100) {
    return 100;
  }
  if (max <= 0) {
    return 1;
  }
  const step = 10 ** Math.floor(Math.log10(max));
  return Number((Math.ceil(max / step) * step).toPrecision(12));
}

export function unitLabel(unit: ResolvedUnit): string {
  switch (unit) {
    case "percent":
      return "%";
    case "count":
      return "";
    default:
      return unit;
  }
}

export function buildSeries(
  entityId: string,
  stats: StateStatistic[],
  data: Statistics,
  unit: ResolvedUnit,
  colors: string[],
  chartType: ChartType = "bar-stack"
): { series: ChartSeries[]; legend: LegendItem[] } {
  const line = chartType.startsWith("line");
  const stacked = chartType.endsWith("stack");
  const series: ChartSeries[] = [];
  const legend: LegendItem[] = [];
  const buckets = new Map<number, number>();
  for (const stat of stats) {
    for (const row of data[stat.statisticId] ?? []) {
      buckets.set(row.start, row.end);
    }
  }
  const starts = [...buckets.keys()].sort((a, b) => a - b);
  stats.forEach((stat, i) => {
    const color = colors[i % colors.length];
    const rows = new Map(
      (data[stat.statisticId] ?? []).map((row) => [row.start, row])
    );
    const points: ChartSeries["data"] = starts.map((start) => {
      const row = rows.get(start);
      const value = row ? valueOf(row, unit) : null;
      return [start, value, start, buckets.get(start)!];
    });
    if (line && points.length) {
      // A line point sits at its bucket's start, so the last bucket has
      // no extent until a point closes it at its end, as the stock chart
      // does (statistics-chart-data.ts:391).
      const last = points[points.length - 1];
      points.push([last[3], last[1], last[2], last[3]]);
    }
    // Fills are translucent so overlapping shapes stay legible; the bar
    // border and the line itself are the solid colour, as the stock chart
    // draws them.
    const styled: ChartSeries = line
      ? {
          id: stat.statisticId,
          name: stat.label,
          type: "line",
          color,
          lineStyle: { width: 1.5 },
          smooth: 0.4,
          symbol: "none",
          cursor: "default",
          animationDurationUpdate: 0,
          data: points,
        }
      : {
          id: stat.statisticId,
          name: stat.label,
          type: "bar",
          color: color + "7F",
          itemStyle: { borderColor: color, borderWidth: 1.5 },
          cursor: "default",
          animationDurationUpdate: 0,
          data: points,
        };
    if (stacked) {
      styled.stack = entityId;
      styled.stackStrategy = "samesign";
      if (line) {
        styled.areaStyle = { color: color + "3F" };
      }
    }
    series.push(styled);
    legend.push({
      id: stat.statisticId,
      name: stat.label,
      itemStyle: { color },
      noLabelClick: true,
    });
  });
  return { series, legend };
}
