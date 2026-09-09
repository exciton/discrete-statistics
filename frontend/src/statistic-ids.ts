import type { Metric, StateSetting, StatisticsMetaData } from "./types";

const DOMAIN = "discrete_statistics";
const METRICS: readonly Metric[] = ["duration", "count"];

export interface StateStatistic {
  statisticId: string;
  token: string;
  label: string;
  metric: Metric;
  // As configured, unresolved; absent for a palette colour.
  color?: string;
}

export const settingState = (setting: StateSetting): string =>
  typeof setting === "string" ? setting : setting.state;

// A `states:` or `ignore_states:` entry names a statistic by token, or —
// since non-Latin text has no token — by the statistic's label, the raw
// state text.
export const settingMatches = (setting: StateSetting, s: StateStatistic): boolean => {
  const entry = settingState(setting);
  return stateToken(entry) === s.token || entry === s.label;
};

// The integration slugifies with python-slugify. Entity IDs are already
// [a-z0-9_.], so lower-casing and collapsing runs of anything else is the
// same answer for them. States can hold anything, and this approximation
// only agrees with python-slugify on ASCII: non-Latin text (e.g. "打开")
// transliterates on the Python side but collapses to "" here. A filter
// entry written in that state's own script therefore cannot be matched by
// token at all — statisticsForEntity falls back to comparing it against
// the statistic's label, the raw state text carried in its stored name.
const slug = (text: string, separator: string): string => {
  const collapsed = text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, separator);
  // A `+` quantifier over an empty separator has nothing to repeat, and
  // trimming is a no-op then anyway — the collapse above already leaves
  // no separator character to strip.
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

// The entities, of those given, that at least one of the integration's
// statistics belongs to — what the editor offers, since only those can
// draw anything. Order is the caller's.
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
      metric,
    });
  }
  if (!filter?.states && !filter?.ignore_states) {
    return found;
  }
  const ignoreList = filter.ignore_states ?? [];
  const kept = found.filter((s) => !ignoreList.some((entry) => settingMatches(entry, s)));
  const listed = (filter.states ?? [])
    .map((setting) => {
      const s = kept.find((found) => settingMatches(setting, found));
      const color = typeof setting === "string" ? undefined : setting.color;
      return s && color ? { ...s, color } : s;
    })
    .filter((s): s is StateStatistic => s !== undefined);
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
