import { bucketHours, type ResolvedUnit } from "./period";
import type { StateStatistic } from "./statistic-ids";
import type { StatisticValue, Statistics } from "./types";

export interface SeriesPoint {
  time: number;
  value: number;
  start: number;
  end: number;
}

export interface ChartSeries {
  // subset of echarts BarSeriesOption the card sets
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
  // name the bucket the bar represents.
  data: [number, number, number, number][];
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
  stats.forEach((stat, i) => {
    const color = colors[i % colors.length];
    const points: ChartSeries["data"] = [];
    for (const row of data[stat.statisticId] ?? []) {
      const value = valueOf(row, unit);
      if (value === null) {
        continue;
      }
      points.push([row.start, value, row.start, row.end]);
    }
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
