// Which of the card's two shapes a config is in, whether it is a legal one
// of them, and how the editor's mode selector rewrites it. The card names
// one entity and its rows are that entity's states, or it names none and
// every row names its own.
import { resolveSeries, settingMatches, statisticsForEntity } from "./statistic-ids";
import type { CardConfig, Metric, StateSetting, StatisticsMetaData } from "./types";

const named = (setting: StateSetting) =>
  typeof setting === "string" ? undefined : setting.entity;

export const isMultiEntity = (config: CardConfig): boolean => !config.entity;

export const isEmpty = (config: CardConfig): boolean =>
  !config.entity && !config.states?.length;

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

// Every row naming the entity taken, and the rest of its states ignored, so
// the card draws what it drew and the editor's list still opens to new ones.
export function toSingleEntity(
  config: CardConfig,
  metric: Metric,
  metadata: StatisticsMetaData[]
): CardConfig {
  const { states, ignore_states: _ignored, ...rest } = config;
  const rows = (states ?? []).filter(
    (setting): setting is Exclude<StateSetting, string> => !!named(setting)
  );
  const entity = rows[0]?.entity;
  if (!entity) {
    return rest as CardConfig;
  }
  const mine = rows.filter((row) => row.entity === entity);
  const drawn: StateSetting[] = mine.map(({ entity: _e, ...keep }) =>
    keep.name || keep.color ? keep : keep.state
  );
  const missing = statisticsForEntity(entity, metric, metadata).filter(
    (series) => !mine.some((row) => settingMatches(row, series))
  );
  return {
    ...rest,
    entity,
    states: drawn,
    ignore_states: missing.map((series) => series.token || series.label),
  } as CardConfig;
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
  return config.entity ? config : toSingleEntity(config, metric, metadata);
}
