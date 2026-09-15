// Which of the card's two shapes a config is in, whether it is a legal one
// of them, and how the editor's mode selector rewrites it. The card names
// one entity and its rows are that entity's states, or it names none and
// every row names its own.
import { resolveSeries, settingMatches } from "./statistic-ids";
import type { CardConfig, Metric, StateSetting, StatisticsMetaData } from "./types";

const named = (setting: StateSetting) =>
  typeof setting === "string" ? undefined : setting.entity;

export const isMultiEntity = (config: CardConfig): boolean => !config.entity;

export function validateConfig(config: CardConfig): void {
  const rows = config.states ?? [];
  if (!isMultiEntity(config)) {
    const row = rows.find((setting) => named(setting));
    if (row) {
      throw new Error(
        `This card already draws ${config.entity}, so a states: entry cannot name ` +
          `${named(row)} as well. Remove the card's entity: to draw several.`
      );
    }
    return;
  }
  if (config.ignore_states) {
    throw new Error(
      "ignore_states: needs the card's own entity:. With one state per entity there is nothing to filter."
    );
  }
  if (rows.some((setting) => !named(setting))) {
    throw new Error(
      "Every states: entry needs an entity: when the card does not name one itself."
    );
  }
}

export function toMultiEntity(
  config: CardConfig,
  metric: Metric,
  metadata: StatisticsMetaData[]
): CardConfig {
  const { entity, states, ignore_states: _ignored, ...rest } = config;
  const drawn = entity ? resolveSeries(config, metric, metadata) : [];
  if (!entity || !drawn.length) {
    return rest as CardConfig;
  }
  const rows = drawn.map((series) => {
    const setting = (states ?? []).find((entry) => settingMatches(entry, series));
    const carried = typeof setting === "object" ? setting : undefined;
    return {
      entity,
      state: series.token,
      ...(carried?.name ? { name: carried.name } : {}),
      ...(carried?.color ? { color: carried.color } : {}),
    };
  });
  return { ...rest, states: rows } as CardConfig;
}

export function toSingleEntity(config: CardConfig): CardConfig {
  const { states, ignore_states: _ignored, ...rest } = config;
  const entity = (states ?? []).map(named).find((id) => id);
  return (entity ? { ...rest, entity } : rest) as CardConfig;
}

export type ChartMode = "states" | "entities";

export function applyChartMode(
  config: CardConfig,
  mode: ChartMode,
  metric: Metric,
  metadata: StatisticsMetaData[]
): CardConfig {
  if (mode === "entities") {
    return isMultiEntity(config) ? config : toMultiEntity(config, metric, metadata);
  }
  return config.entity ? config : toSingleEntity(config);
}
