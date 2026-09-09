import { describe, expect, it } from "vitest";
import { configSchema } from "../src/editor";

describe("configSchema", () => {
  it("limits the entity picker to the entities given", () => {
    const [entity] = configSchema(["climate.zone"]);
    expect(entity.selector).toEqual({
      entity: { include_entities: ["climate.zone"] },
    });
  });

  it("offers every entity when the list is unknown", () => {
    const [entity] = configSchema(undefined);
    expect(entity.selector).toEqual({ entity: {} });
  });
});
