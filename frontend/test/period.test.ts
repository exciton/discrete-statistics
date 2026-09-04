import { describe, expect, it } from "vitest";
import {
  bucketHours,
  rangeFromDays,
  resolvePeriod,
  resolveUnit,
  suggestPeriod,
} from "../src/period";

const day = 24 * 3600 * 1000;
const range = (days: number) => {
  const end = new Date("2026-06-15T12:34:56Z");
  return { start: new Date(end.getTime() - days * day), end };
};

describe("rangeFromDays", () => {
  it("ends now and starts on the hour, days_to_show earlier", () => {
    const now = new Date("2026-06-15T12:34:56Z");
    const r = rangeFromDays(7, now);
    expect(r.end).toEqual(now);
    expect(r.start).toEqual(new Date("2026-06-08T12:00:00Z"));
  });
});

describe("suggestPeriod", () => {
  it("picks the coarsest period that still gives a few bars", () => {
    expect(suggestPeriod(range(1))).toBe("hour");
    expect(suggestPeriod(range(2))).toBe("hour");
    expect(suggestPeriod(range(3))).toBe("day");
    expect(suggestPeriod(range(14))).toBe("day");
    expect(suggestPeriod(range(15))).toBe("week");
    expect(suggestPeriod(range(70))).toBe("week");
    expect(suggestPeriod(range(71))).toBe("month");
    expect(suggestPeriod(range(731))).toBe("month");
    expect(suggestPeriod(range(732))).toBe("year");
  });
});

describe("resolvePeriod", () => {
  it("keeps an explicit period and suggests for auto or absent", () => {
    expect(resolvePeriod("month", range(3))).toBe("month");
    expect(resolvePeriod("auto", range(3))).toBe("day");
    expect(resolvePeriod(undefined, range(3))).toBe("day");
  });
});

describe("bucketHours", () => {
  it("is the wall-clock length of the row, so a DST day is 23 or 25", () => {
    const start = new Date("2026-03-29T00:00:00+01:00").getTime();
    const end = new Date("2026-03-30T00:00:00+02:00").getTime();
    expect(bucketHours({ start, end })).toBe(23);
    expect(bucketHours({ start: 0, end: 3600 * 1000 })).toBe(1);
  });
});

describe("resolveUnit", () => {
  it("counts are counts whatever the unit says", () => {
    expect(resolveUnit("percent", "count", "day")).toBe("count");
    expect(resolveUnit(undefined, "count", "day")).toBe("count");
  });

  it("auto is hours for fine buckets and days for coarse ones", () => {
    expect(resolveUnit("auto", "duration", "hour")).toBe("h");
    expect(resolveUnit("auto", "duration", "day")).toBe("h");
    expect(resolveUnit(undefined, "duration", "day")).toBe("h");
    expect(resolveUnit("auto", "duration", "week")).toBe("d");
    expect(resolveUnit("auto", "duration", "month")).toBe("d");
    expect(resolveUnit("auto", "duration", "year")).toBe("d");
  });

  it("explicit units stand", () => {
    expect(resolveUnit("h", "duration", "year")).toBe("h");
    expect(resolveUnit("d", "duration", "hour")).toBe("d");
    expect(resolveUnit("percent", "duration", "hour")).toBe("percent");
  });
});
