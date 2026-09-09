import { describe, expect, it } from "vitest";
import { configSchema } from "../src/editor";

// The form is nested grids; this walks it for a field by name.
function field(schema: unknown[], name: string): Record<string, unknown> | undefined {
  for (const item of schema as Record<string, unknown>[]) {
    if (item.name === name) {
      return item;
    }
    if (Array.isArray(item.schema)) {
      const found = field(item.schema, name);
      if (found) {
        return found;
      }
    }
  }
  return undefined;
}

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

  it("offers the stock card's chart types under its key, as radio buttons", () => {
    const chartType = field(configSchema(), "chart_type")!;
    const select = (
      chartType.selector as { select: { mode: string; options: { value: string }[] } }
    ).select;
    expect(select.mode).toBe("list");
    expect(select.options.map((o) => o.value)).toEqual([
      "line", "line-stack", "bar", "bar-stack",
    ]);
  });

  it("lists the periods as radio buttons beside the chart type", () => {
    const schema = configSchema();
    const period = field(schema, "period")!;
    expect((period.selector as { select: { mode: string } }).select.mode).toBe(
      "list"
    );
    const grid = (schema as Record<string, unknown>[]).find(
      (item) => item.type === "grid"
    )!;
    const columns = grid.schema as Record<string, unknown>[];
    expect(columns[1].name).toBe("period");
    expect(field([columns[0]], "chart_type")).toBeDefined();
    expect(field([columns[0]], "days_to_show")).toBeDefined();
  });

  it("never lets the metric or unit be cleared", () => {
    const schema = configSchema();
    expect(field(schema, "metric")!.required).toBe(true);
    expect(field(schema, "unit")!.required).toBe(true);
  });

  it("hides the time unit while the metric is a count", () => {
    expect(field(configSchema(), "unit")!.visible).toEqual({
      field: "metric",
      operator: "not_eq",
      value: "count",
    });
  });

  it("swaps days to show for the collection key while following the picker", () => {
    const following = configSchema(undefined, true);
    expect(field(following, "days_to_show")).toBeUndefined();
    expect(field(following, "collection_key")).toBeDefined();
    expect(field(configSchema(undefined, false), "collection_key")).toBeUndefined();
  });
});
