import { bucketHours, type ResolvedUnit } from "./period";
import type { StateStatistic } from "./statistic-ids";
import type { StatisticValue, Statistics } from "./types";

export interface ChartSeries {
  // The fields of a bar series ha-chart-base is given; it types them itself,
  // this is only what the card sets.
  id: string;
  name: string;
  type: "bar";
  stack: string;
  stackStrategy: "samesign";
  color: string;
  itemStyle: { borderColor: string; borderWidth: number };
  cursor: "default";
  animationDurationUpdate: 0;
  // [bar time, value, bucket start, bucket end], as the stock statistics
  // chart draws bars (statistics-chart-data.ts:198), so a tooltip can
  // name the bucket the bar represents. Every series holds every bucket
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
  colors: string[]
): { series: ChartSeries[]; legend: LegendItem[] } {
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
    series.push({
      id: stat.statisticId,
      name: stat.label,
      type: "bar",
      stack: entityId,
      stackStrategy: "samesign",
      // Fill is translucent (alpha 7F) so overlapping stacked bars stay
      // legible; the border is the solid colour, as the stock bar chart
      // draws it.
      color: color + "7F",
      itemStyle: { borderColor: color, borderWidth: 1.5 },
      cursor: "default",
      animationDurationUpdate: 0,
      data: points,
    });
    legend.push({
      id: stat.statisticId,
      name: stat.label,
      itemStyle: { color },
      noLabelClick: true,
    });
  });
  return { series, legend };
}
