import { describe, expect, it } from "vitest";
import { automaticIndex, stateList, stateListConfig } from "../src/state-list";
import type { StateStatistic } from "../src/statistic-ids";

const stat = (token: string, label = token): StateStatistic => ({
  statisticId: `discrete_statistics:climate_zone_${token}_duration`,
  token,
  label,
  metric: "duration",
});
const all = [stat("off"), stat("heat"), stat("cool"), stat("", "打开")];

describe("stateList", () => {
  it("with no filter shows every state by label and lets new ones through", () => {
    expect(stateList(all, {})).toEqual({
      rows: [
        { token: "cool", label: "cool", shown: true },
        { token: "heat", label: "heat", shown: true },
        { token: "off", label: "off", shown: true },
        { token: "", label: "打开", shown: true },
      ],
      ignoreNew: false,
    });
  });

  it("states: alone leads with the listed order and unticks the rest", () => {
    const list = stateList(all, {
      states: [{ state: "heat", name: "Heating", color: "red" }, "off"],
    });
    expect(list.ignoreNew).toBe(true);
    expect(list.rows).toEqual([
      { token: "heat", label: "heat", shown: true, name: "Heating", color: "red" },
      { token: "off", label: "off", shown: true },
      { token: "cool", label: "cool", shown: false },
      { token: "", label: "打开", shown: false },
    ]);
  });

  it("ignore_states: unticks what it names and lets new states through", () => {
    const list = stateList(all, { states: ["heat"], ignore_states: ["off", "打开"] });
    expect(list.ignoreNew).toBe(false);
    expect(list.rows.map((r) => [r.token, r.shown])).toEqual([
      ["heat", true],
      ["cool", true],
      ["off", false],
      ["", false],
    ]);
  });

  it("ignores entries that match nothing, and a repeat", () => {
    const list = stateList(all, { states: ["auto", "heat", "Heat"] });
    expect(list.rows.map((r) => r.token)).toEqual(["heat", "cool", "off", ""]);
  });
});

describe("stateListConfig", () => {
  const rows = [
    { token: "heat", label: "heat", shown: true, name: "Heating", color: "red" },
    { token: "off", label: "off", shown: true, color: "blue" },
    { token: "cool", label: "cool", shown: false },
    { token: "", label: "打开", shown: false },
  ];

  it("ignoring new states writes the ticked rows as states: alone", () => {
    expect(stateListConfig({ rows, ignoreNew: true })).toEqual({
      states: [
        { state: "heat", name: "Heating", color: "red" },
        { state: "off", color: "blue" },
      ],
    });
  });

  it("allowing new states writes the unticked rows as ignore_states:", () => {
    expect(stateListConfig({ rows, ignoreNew: false })).toEqual({
      states: [
        { state: "heat", name: "Heating", color: "red" },
        { state: "off", color: "blue" },
      ],
      ignore_states: ["cool", "打开"],
    });
  });

  it("allowing new states with every row ticked writes an empty ignore_states:", () => {
    // The key's presence is what opens the list.
    const shown = rows.map((r) => ({ ...r, shown: true }));
    expect(stateListConfig({ rows: shown, ignoreNew: false })).toEqual({
      states: [
        { state: "heat", name: "Heating", color: "red" },
        { state: "off", color: "blue" },
        "cool",
        "打开",
      ],
      ignore_states: [],
    });
  });

  it("reads back what it wrote", () => {
    for (const ignoreNew of [true, false]) {
      const list = { rows, ignoreNew };
      expect(stateList(all, stateListConfig(list))).toEqual(list);
    }
  });
});

describe("automaticIndex", () => {
  const rows = [
    { token: "heat", label: "heat", shown: true },
    { token: "off", label: "off", shown: false },
    { token: "cool", label: "cool", shown: true },
    { token: "fan", label: "fan", shown: false },
  ];

  it("counts a drawn row's position among the drawn rows, as the chart does", () => {
    expect(automaticIndex(rows, 0)).toBe(0);
    expect(automaticIndex(rows, 2)).toBe(1);
  });

  it("gives an undrawn row the position it would take if ticked", () => {
    expect(automaticIndex(rows, 1)).toBe(1);
    expect(automaticIndex(rows, 3)).toBe(2);
  });
});
