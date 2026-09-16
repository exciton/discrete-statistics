import type { CardConfig, Metric, StateSetting, StatisticsMetaData } from "./types";

const DOMAIN = "discrete_statistics";
const METRICS: readonly Metric[] = ["duration", "count"];

// A state as the editor's dropdown offers it: the statistic's token, and
// the state label drawn for it.
export interface StateOption {
  value: string;
  label: string;
}

export interface StateStatistic {
  statisticId: string;
  token: string;
  // The stored name, or the configured one in its place.
  label: string;
  entityLabel?: string;
  metric: Metric;
  // As configured, unresolved; absent for a palette colour.
  color?: string;
}

const settingState = (setting: StateSetting): string =>
  typeof setting === "string" ? setting : setting.state;

// An entry names a statistic by token, or — since non-Latin text has no
// token — by its label, the raw state text.
export const settingMatches = (setting: StateSetting, s: StateStatistic): boolean => {
  const entry = settingState(setting);
  return stateToken(entry) === s.token || entry === s.label;
};

// An approximation of the integration's python-slugify. Exact for entity
// IDs, already [a-z0-9_.]; for states it agrees only on ASCII, since
// non-Latin text (e.g. "打开") transliterates on the Python side but
// collapses to "" here. Hence the label fallback in settingMatches: a
// filter entry in such a state's own script can never match by token.
const slug = (text: string, separator: string): string => {
  const collapsed = text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, separator);
  // A `+` over an empty separator has nothing to repeat, and there is
  // nothing left to trim anyway.
  return separator
    ? collapsed.replace(new RegExp(`^${separator}+|${separator}+$`, "g"), "")
    : collapsed;
};

export const entitySlug = (entityId: string): string => slug(entityId, "_");

export const stateToken = (state: string): string => slug(state, "");

export function parseStatisticId(
  id: string
): { entitySlug: string; token: string; metric: Metric } | null {
  const colon = id.indexOf(":");
  if (colon < 0 || id.slice(0, colon) !== DOMAIN) {
    return null;
  }
  const parts = id.slice(colon + 1).split("_");
  if (parts.length < 3) {
    return null;
  }
  const metric = parts[parts.length - 1] as Metric;
  const token = parts[parts.length - 2];
  const entity = parts.slice(0, -2).join("_");
  if (!METRICS.includes(metric) || !token || !entity) {
    return null;
  }
  return { entitySlug: entity, token, metric };
}

// A stored name is "<display>: <State> (h)" — see payload.compose_name.
// The display half may hold colons; the state half never does.
export function stateLabel(
  meta: StatisticsMetaData | undefined,
  token: string
): string {
  const name = meta?.name;
  if (!name) {
    return token;
  }
  const sep = name.lastIndexOf(": ");
  if (sep <= 0) {
    return token;
  }
  const tail = name.slice(sep + 2).replace(/ \((h|#)\)$/, "");
  return tail || token;
}

export function entityLabel(
  meta: StatisticsMetaData | undefined,
  entityId: string
): string {
  const name = meta?.name;
  if (!name) {
    return entityId;
  }
  const sep = name.lastIndexOf(": ");
  return sep > 0 ? name.slice(0, sep) : entityId;
}

// What the editor offers: only an entity with statistics can draw
// anything. Order is the caller's.
export function entitiesWithStatistics(
  entityIds: string[],
  metadata: StatisticsMetaData[]
): string[] {
  const slugs = new Set<string>();
  for (const meta of metadata) {
    const parsed = parseStatisticId(meta.statistic_id);
    if (parsed) {
      slugs.add(parsed.entitySlug);
    }
  }
  return entityIds.filter((id) => slugs.has(entitySlug(id)));
}

export function stateOptionsFor(
  entityIds: string[],
  metric: Metric,
  metadata: StatisticsMetaData[]
): Record<string, StateOption[]> {
  const bySlug = new Map<string, StateOption[]>();
  for (const meta of metadata) {
    const parsed = parseStatisticId(meta.statistic_id);
    if (!parsed || parsed.metric !== metric) {
      continue;
    }
    const option = { value: parsed.token, label: stateLabel(meta, parsed.token) };
    const options = bySlug.get(parsed.entitySlug);
    if (options) {
      options.push(option);
    } else {
      bySlug.set(parsed.entitySlug, [option]);
    }
  }
  const map: Record<string, StateOption[]> = {};
  for (const entityId of entityIds) {
    const options = bySlug.get(entitySlug(entityId));
    if (options) {
      map[entityId] = options;
    }
  }
  return map;
}

// Two rows can resolve to one statistic — the same row twice, or two states
// that tokenise alike — and one series drawn twice fails the chart.
const firstOfEach = <T>(items: T[], id: (item: T) => string): T[] => {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = id(item);
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
};

export function statisticsForEntity(
  entityId: string,
  metric: Metric,
  metadata: StatisticsMetaData[],
  filter?: { states?: StateSetting[]; ignore_states?: string[] }
): StateStatistic[] {
  const wanted = entitySlug(entityId);
  const found: StateStatistic[] = [];
  for (const meta of metadata) {
    const parsed = parseStatisticId(meta.statistic_id);
    if (!parsed || parsed.entitySlug !== wanted || parsed.metric !== metric) {
      continue;
    }
    found.push({
      statisticId: meta.statistic_id,
      token: parsed.token,
      label: stateLabel(meta, parsed.token),
      entityLabel: entityLabel(meta, entityId),
      metric,
    });
  }
  if (!filter?.states && !filter?.ignore_states) {
    return found;
  }
  const ignoreList = filter.ignore_states ?? [];
  const kept = found.filter((s) => !ignoreList.some((entry) => settingMatches(entry, s)));
  const listed = firstOfEach(
    (filter.states ?? [])
      .map((setting) => {
        const s = kept.find((found) => settingMatches(setting, found));
        if (!s || typeof setting === "string") {
          return s;
        }
        return {
          ...s,
          ...(setting.name ? { label: setting.name } : {}),
          ...(setting.color ? { color: setting.color } : {}),
        };
      })
      .filter((s): s is StateStatistic => s !== undefined),
    (s) => s.statisticId
  );
  // `states:` alone is a closed list. `ignore_states:` opens it: a state
  // the entity gains later is appended rather than silently dropped.
  if (!filter.ignore_states) {
    return listed;
  }
  const seen = new Set(listed.map((s) => s.token));
  const rest = kept
    .filter((s) => !seen.has(s.token))
    .sort((a, b) => a.label.localeCompare(b.label));
  return [...listed, ...rest];
}

export function statisticsForRows(
  rows: StateSetting[],
  metric: Metric,
  metadata: StatisticsMetaData[]
): StateStatistic[] {
  const found: { setting: Exclude<StateSetting, string>; stat: StateStatistic }[] = [];
  for (const setting of rows) {
    if (typeof setting === "string" || !setting.entity) {
      continue;
    }
    const stat = statisticsForEntity(setting.entity, metric, metadata).find(
      (candidate) => settingMatches(setting, candidate)
    );
    if (stat) {
      found.push({ setting, stat });
    }
  }
  const unique = firstOfEach(found, ({ stat }) => stat.statisticId);
  const counts = new Map<string, number>();
  for (const { setting } of unique) {
    counts.set(setting.entity!, (counts.get(setting.entity!) ?? 0) + 1);
  }
  return unique.map(({ setting, stat }) => {
    const entity = stat.entityLabel ?? setting.entity!;
    const name =
      setting.name ??
      (counts.get(setting.entity!)! > 1 ? `${entity}: ${stat.label}` : entity);
    return {
      ...stat,
      label: name,
      ...(setting.color ? { color: setting.color } : {}),
    };
  });
}

export type SeriesConfig = Pick<CardConfig, "entity" | "states" | "ignore_states">;

export function resolveSeries(
  config: SeriesConfig,
  metric: Metric,
  metadata: StatisticsMetaData[]
): StateStatistic[] {
  return config.entity
    ? statisticsForEntity(config.entity, metric, metadata, {
        states: config.states,
        ignore_states: config.ignore_states,
      })
    : statisticsForRows(config.states ?? [], metric, metadata);
}
