import { describe, expect, it } from "vitest";
import {
  automaticIndex,
  rowsAfterEntityChange,
  seriesList,
  seriesListConfig,
  stateList,
  stateListConfig,
  type StateRow,
} from "../src/state-list";
import type { StateStatistic } from "../src/statistic-ids";
import type { StateSetting, StatisticsMetaData } from "../src/types";

const meta = (id: string, name: string): StatisticsMetaData => ({
  statistic_id: id,
  source: "discrete_statistics",
  name,
  statistics_unit_of_measurement: id.endsWith("duration") ? "h" : null,
  has_sum: true,
  unit_class: id.endsWith("duration") ? "duration" : null,
});

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
      mode: "states",
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
    expect(stateListConfig({ mode: "states", rows, ignoreNew: true })).toEqual({
      states: [
        { state: "heat", name: "Heating", color: "red" },
        { state: "off", color: "blue" },
      ],
    });
  });

  it("allowing new states writes the unticked rows as ignore_states:", () => {
    expect(stateListConfig({ mode: "states", rows, ignoreNew: false })).toEqual({
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
    expect(stateListConfig({ mode: "states", rows: shown, ignoreNew: false })).toEqual({
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
      const list = { mode: "states" as const, rows, ignoreNew };
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

describe("seriesList", () => {
  const metadata = [
    meta("discrete_statistics:cover_gate_open_duration", "Gate: Open (h)"),
    meta("discrete_statistics:binary_sensor_hall_motion_on_duration", "Hall Motion: Detected (h)"),
  ];

  it("keeps the rows as configured, labelled by their entities", () => {
    expect(
      seriesList(
        [
          { entity: "cover.gate", state: "open", name: "Gate open", color: "red" },
          { entity: "binary_sensor.hall_motion", state: "on" },
        ],
        "duration",
        metadata
      )
    ).toEqual({
      mode: "entities",
      ignoreNew: false,
      rows: [
        {
          token: "open",
          label: "Gate",
          entity: "cover.gate",
          shown: true,
          name: "Gate open",
          color: "red",
        },
        {
          token: "on",
          label: "Hall Motion",
          entity: "binary_sensor.hall_motion",
          shown: true,
        },
      ],
    });
  });

  it("keeps a row whose statistic is missing, under its entity ID", () => {
    const [row] = seriesList([{ entity: "light.nowhere", state: "on" }], "duration", metadata).rows;
    expect(row).toEqual({ token: "on", label: "light.nowhere", entity: "light.nowhere", shown: true });
  });

  it("round-trips back to config", () => {
    const rows: StateSetting[] = [
      { entity: "cover.gate", state: "open", name: "Gate open", color: "red" },
      { entity: "binary_sensor.hall_motion", state: "on" },
    ];
    expect(seriesListConfig(seriesList(rows, "duration", metadata))).toEqual({ states: rows });
  });

  it("writes back the matched statistic's token for a state with no ASCII token", () => {
    const withTransliterated = [
      ...metadata,
      meta("discrete_statistics:cover_gate_dakai_duration", "Gate: 打开 (h)"),
    ];
    const list = seriesList([{ entity: "cover.gate", state: "打开" }], "duration", withTransliterated);
    const config = seriesListConfig(list);
    expect(config.states).toEqual([{ entity: "cover.gate", state: "dakai" }]);
    const [row] = seriesList(config.states!, "duration", withTransliterated).rows;
    expect(row.entity).toBe("cover.gate");
    expect(row.token).toBe("dakai");
  });

  it("keeps a non-ASCII state's own text when it matches nothing", () => {
    const config = seriesListConfig(
      seriesList([{ entity: "light.nowhere", state: "打开" }], "duration", metadata)
    );
    expect(config.states).toEqual([{ entity: "light.nowhere", state: "打开" }]);
  });
});

describe("rowsAfterEntityChange", () => {
  const rows: StateRow[] = [
    { token: "open", label: "Gate", entity: "cover.gate", shown: true },
    { token: "on", label: "Hall", entity: "binary_sensor.hall_motion", shown: true },
  ];
  const gate = [
    { value: "open", label: "Open" },
    { value: "closed", label: "Closed" },
  ];
  const hall = [
    { value: "on", label: "On" },
    { value: "off", label: "Off" },
  ];

  it("appends a row in the entity's first state", () => {
    expect(rowsAfterEntityChange(rows, 2, "light.hall", hall)).toEqual([
      ...rows,
      { token: "on", label: "light.hall", entity: "light.hall", shown: true },
    ]);
  });

  it("gives a second row for the same entity the second state", () => {
    const one = rowsAfterEntityChange([], 0, "cover.gate", gate);
    expect(one[0].token).toBe("open");
    expect(rowsAfterEntityChange(one, 1, "cover.gate", gate)[1].token).toBe("closed");
  });

  it("is not blocked by a row naming a different entity", () => {
    expect(rowsAfterEntityChange(rows, 2, "light.hall", hall)[2].token).toBe("on");
  });

  it("takes no state when the entity's every state is taken", () => {
    const both = rowsAfterEntityChange(
      rowsAfterEntityChange([], 0, "cover.gate", gate),
      1,
      "cover.gate",
      gate
    );
    expect(rowsAfterEntityChange(both, 2, "cover.gate", gate)[2].token).toBe("");
  });

  it("takes no state when the entity has no statistics", () => {
    expect(rowsAfterEntityChange(rows, 2, "light.hall", [])).toEqual([
      ...rows,
      { token: "", label: "light.hall", entity: "light.hall", shown: true },
    ]);
  });

  it("removes the row whose entity was cleared", () => {
    expect(rowsAfterEntityChange(rows, 0, undefined, [])).toEqual([rows[1]]);
  });

  it("leaves the rows alone when the picker past the end is cleared", () => {
    expect(rowsAfterEntityChange(rows, 2, undefined, [])).toEqual(rows);
  });

  it("takes a state from the new entity, never the old one's", () => {
    expect(rowsAfterEntityChange(rows, 0, "light.hall", hall)).toEqual([
      { token: "on", label: "light.hall", entity: "light.hall", shown: true },
      rows[1],
    ]);
  });

  it("does not let the row being changed block its own state", () => {
    expect(rowsAfterEntityChange(rows, 0, "cover.gate", gate)[0].token).toBe("open");
  });

  it("keeps the name and colour the row carried", () => {
    const named: StateRow[] = [{ ...rows[0], name: "Gate open", color: "red" }];
    expect(rowsAfterEntityChange(named, 0, "light.hall", hall)).toEqual([
      {
        token: "on",
        label: "light.hall",
        entity: "light.hall",
        shown: true,
        name: "Gate open",
        color: "red",
      },
    ]);
  });
});
