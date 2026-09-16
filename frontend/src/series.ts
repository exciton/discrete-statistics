import { bucketHours, type ResolvedUnit } from "./period";
import type { StateStatistic } from "./statistic-ids";
import type { ChartType, StatisticValue, Statistics } from "./types";

export interface ChartSeries {
  // ha-chart-base types a series itself; these are the fields the card sets.
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
  // chart draws bars (statistics-chart-data.ts:198), so a tooltip can name
  // the bucket a point represents. Every series holds every bucket in the
  // same order, null where it has no value: ECharts stacks on a time axis
  // by data index, not x value, so a series missing a bucket would stack
  // its later bars on the wrong base.
  data: [number, number | null, number, number][];
}

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
      // count or the sum over states: a 23- or 25-hour DST bucket must
      // still read 100% when the entity held one state throughout.
      return (100 * change) / bucketHours(row);
    default:
      return change;
  }
}

// The tallest visible stack rounded up to its own order of magnitude — 37
// to 40, 0.032 to 0.04 — so the axis follows what the legend leaves
// showing however small that is.
export function roundUpMax({ max }: { min: number; max: number }): number {
  if (max <= 0) {
    return 1;
  }
  const step = 10 ** Math.floor(Math.log10(max));
  return Number((Math.ceil(max / step) * step).toPrecision(12));
}

// Capped rather than rounded at 100 because a full stack's float sum can
// land a hair over, pushing the axis to 110. Several entities' shares of a
// period can exceed it honestly, and that chart uses roundUpMax.
export function percentAxisMax(bounds: { min: number; max: number }): number {
  return Math.min(100, roundUpMax(bounds));
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
  stack: string,
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
    // Six-digit hex either way, which is what the alpha suffixes below need.
    const color = stat.color ?? colors[i % colors.length];
    const rows = new Map(
      (data[stat.statisticId] ?? []).map((row) => [row.start, row])
    );
    const points: ChartSeries["data"] = starts.map((start) => {
      const row = rows.get(start);
      const value = row ? valueOf(row, unit) : null;
      return [start, value, start, buckets.get(start)!];
    });
    if (line && points.length) {
      // A line point sits at its bucket's start, so the last bucket has no
      // extent until a point closes it (statistics-chart-data.ts:391).
      const last = points[points.length - 1];
      points.push([last[3], last[1], last[2], last[3]]);
    }
    // Translucent fills so overlapping shapes stay legible, solid borders
    // and lines, as the stock chart draws them.
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
      styled.stack = stack;
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
