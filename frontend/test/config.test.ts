import { describe, expect, it } from "vitest";
import {
  applyChartMode,
  isEmpty,
  isMultiEntity,
  type ChartMode,
  toMultiEntity,
  toSingleEntity,
  validateConfig,
} from "../src/config";
import type { CardConfig, StatisticsMetaData } from "../src/types";

const config = (over: Partial<CardConfig>): CardConfig => ({
  type: "custom:discrete-statistics-card",
  ...over,
}) as CardConfig;

const meta = (id: string, name: string): StatisticsMetaData => ({
  statistic_id: id,
  source: "discrete_statistics",
  name,
  statistics_unit_of_measurement: "h",
  has_sum: true,
  unit_class: "duration",
});
const metadata = [
  meta("discrete_statistics:cover_gate_open_duration", "Gate: Open (h)"),
  meta("discrete_statistics:cover_gate_closed_duration", "Gate: Closed (h)"),
  meta("discrete_statistics:cover_gate_opening_duration", "Gate: Opening (h)"),
];

describe("isMultiEntity", () => {
  it("is the absence of the card's own entity", () => {
    expect(isMultiEntity(config({ entity: "cover.gate" }))).toBe(false);
    expect(isMultiEntity(config({ states: [{ entity: "cover.gate", state: "open" }] }))).toBe(true);
  });
});

describe("isEmpty", () => {
  it("is a card with neither an entity nor rows", () => {
    expect(isEmpty(config({}))).toBe(true);
    expect(isEmpty(config({ states: [] }))).toBe(true);
    expect(isEmpty(config({ entity: "cover.gate" }))).toBe(false);
    expect(isEmpty(config({ states: [{ entity: "cover.gate", state: "open" }] }))).toBe(false);
  });
});

describe("validateConfig", () => {
  it("accepts each shape on its own", () => {
    expect(() => validateConfig(config({ entity: "cover.gate", states: ["open"] }))).not.toThrow();
    expect(() =>
      validateConfig(config({ states: [{ entity: "cover.gate", state: "open" }] }))
    ).not.toThrow();
  });

  it("accepts a card the editor has not filled in yet", () => {
    expect(() => validateConfig(config({}))).not.toThrow();
    expect(() => validateConfig(config({ states: [] }))).not.toThrow();
  });

  it("refuses a row that names an entity when the card already has one", () => {
    expect(() =>
      validateConfig(
        config({ entity: "cover.gate", states: [{ entity: "cover.gate", state: "open" }] })
      )
    ).toThrow(/entity/);
  });

  it("refuses a row that names no entity when the card has none", () => {
    expect(() =>
      validateConfig(config({ states: [{ entity: "cover.gate", state: "open" }, { state: "on" }] }))
    ).toThrow(/entity/);
    expect(() => validateConfig(config({ states: ["on"] }))).toThrow(/entity/);
  });

  it("refuses ignore_states: without the card's own entity", () => {
    expect(() =>
      validateConfig(
        config({ states: [{ entity: "cover.gate", state: "open" }], ignore_states: ["unavailable"] })
      )
    ).toThrow(/ignore_states/);
  });
});

describe("mode flips", () => {
  it("carries the entity into a row per state it has statistics for", () => {
    expect(
      toMultiEntity(config({ entity: "cover.gate", title: "Gate" }), "duration", metadata)
    ).toEqual(
      config({
        title: "Gate",
        states: [
          { entity: "cover.gate", state: "open" },
          { entity: "cover.gate", state: "closed" },
          { entity: "cover.gate", state: "opening" },
        ],
      })
    );
  });

  it("carries every drawn state in order, with the names and colours it had", () => {
    expect(
      toMultiEntity(
        config({
          entity: "cover.gate",
          states: [{ state: "closed", color: "red" }, { state: "open", name: "Ajar" }],
        }),
        "duration",
        metadata
      )
    ).toEqual(
      config({
        states: [
          { entity: "cover.gate", state: "closed", color: "red" },
          { entity: "cover.gate", state: "open", name: "Ajar" },
        ],
      })
    );
  });

  it("carries only what ignore_states: left it drawing", () => {
    expect(
      toMultiEntity(
        config({ entity: "cover.gate", ignore_states: ["opening"] }),
        "duration",
        metadata
      )
    ).toEqual(
      config({
        states: [
          { entity: "cover.gate", state: "closed" },
          { entity: "cover.gate", state: "open" },
        ],
      })
    );
  });

  it("drops the states filter when it carries the first row's entity back", () => {
    expect(
      toSingleEntity(
        config({
          title: "Gate",
          states: [{ entity: "cover.gate", state: "open", color: "red" }],
          ignore_states: [],
        })
      )
    ).toEqual(config({ title: "Gate", entity: "cover.gate" }));
  });

  it("leaves no entity behind when there is none to carry", () => {
    expect(toMultiEntity(config({}), "duration", metadata)).toEqual(config({}));
    expect(toSingleEntity(config({}))).toEqual(config({}));
  });
});

describe("applyChartMode", () => {
  const cases: {
    what: string;
    from: CardConfig;
    mode: ChartMode;
    metadata: StatisticsMetaData[];
    to: CardConfig;
  }[] = [
    {
      what: "carries a single-entity card's states into a row each",
      from: config({ entity: "cover.gate", title: "Gate" }),
      mode: "entities",
      metadata,
      to: config({
        title: "Gate",
        states: [
          { entity: "cover.gate", state: "open" },
          { entity: "cover.gate", state: "closed" },
          { entity: "cover.gate", state: "opening" },
        ],
      }),
    },
    {
      what: "keeps the order and colours a states: filter gave them",
      from: config({
        entity: "cover.gate",
        states: [{ state: "closed", color: "red" }, "open"],
      }),
      mode: "entities",
      metadata,
      to: config({
        states: [
          { entity: "cover.gate", state: "closed", color: "red" },
          { entity: "cover.gate", state: "open" },
        ],
      }),
    },
    {
      what: "carries only the states ignore_states: left drawn",
      from: config({ entity: "cover.gate", ignore_states: ["opening"] }),
      mode: "entities",
      metadata,
      to: config({
        states: [
          { entity: "cover.gate", state: "closed" },
          { entity: "cover.gate", state: "open" },
        ],
      }),
    },
    {
      what: "leaves a single-entity card with no rows when nothing resolves",
      from: config({ entity: "cover.gate", states: ["open"] }),
      mode: "entities",
      metadata: [],
      to: config({}),
    },
    {
      what: "leaves a card already in the mode asked for alone",
      from: config({ states: [{ entity: "cover.gate", state: "open" }] }),
      mode: "entities",
      metadata,
      to: config({ states: [{ entity: "cover.gate", state: "open" }] }),
    },
    {
      what: "takes the first row's entity back",
      from: config({ states: [{ entity: "cover.gate", state: "open" }], title: "Gate" }),
      mode: "states",
      metadata,
      to: config({ title: "Gate", entity: "cover.gate" }),
    },
    {
      what: "answers a rowless multi-entity card with a card asking for an entity",
      from: config({}),
      mode: "states",
      metadata,
      to: config({}),
    },
    {
      what: "drops a filter the cleared entity left behind",
      from: config({ states: ["open", "closed"], title: "Gate" }),
      mode: "states",
      metadata,
      to: config({ title: "Gate" }),
    },
  ];

  for (const { what, from, mode, metadata: meta, to } of cases) {
    it(what, () => {
      expect(applyChartMode(from, mode, "duration", meta)).toEqual(to);
    });
  }

  it("never answers with a config the card would refuse", () => {
    for (const { from, mode, metadata: meta } of cases) {
      expect(() => validateConfig(applyChartMode(from, mode, "duration", meta))).not.toThrow();
    }
  });
});
