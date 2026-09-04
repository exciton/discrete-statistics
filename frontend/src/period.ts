import type { Metric, Period, ResolvedPeriod, Unit } from "./types";

export interface Range {
  start: Date;
  end: Date;
}

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;

export function rangeFromDays(daysToShow: number, now: Date): Range {
  const start = new Date(now.getTime() - daysToShow * DAY);
  start.setUTCMinutes(0, 0, 0);
  return { start, end: now };
}

export function suggestPeriod(range: Range): ResolvedPeriod {
  const days = (range.end.getTime() - range.start.getTime()) / DAY;
  if (days > 731) return "year";
  if (days > 70) return "month";
  if (days > 14) return "week";
  if (days > 2) return "day";
  return "hour";
}

export function resolvePeriod(
  period: Period | undefined,
  range: Range
): ResolvedPeriod {
  return !period || period === "auto" ? suggestPeriod(range) : period;
}

export function bucketHours(row: { start: number; end: number }): number {
  return (row.end - row.start) / HOUR;
}

export type ResolvedUnit = "h" | "d" | "percent" | "count";

export function resolveUnit(
  unit: Unit | undefined,
  metric: Metric,
  period: ResolvedPeriod
): ResolvedUnit {
  if (metric === "count") return "count";
  if (unit && unit !== "auto") return unit;
  return period === "hour" || period === "day" ? "h" : "d";
}
