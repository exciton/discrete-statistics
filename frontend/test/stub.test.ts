import { describe, expect, it } from "vitest";
import { pickStubEntity } from "../src/stub";

describe("pickStubEntity", () => {
  it("prefers an unused entity of a discrete domain, in domain order", () => {
    expect(
      pickStubEntity(["sensor.power", "switch.a", "binary_sensor.b"], ["light.c"]),
    ).toBe("binary_sensor.b");
  });

  it("falls back to the full list when no unused entity fits", () => {
    expect(pickStubEntity(["sensor.power"], ["sensor.temp", "light.c"])).toBe("light.c");
  });

  it("gives nothing when no entity fits at all", () => {
    expect(pickStubEntity(["sensor.power"], ["sensor.temp"])).toBeUndefined();
  });
});
