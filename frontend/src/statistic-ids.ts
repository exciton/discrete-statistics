import type { Metric, StatisticsMetaData } from "./types";

const DOMAIN = "discrete_statistics";
const METRICS: readonly Metric[] = ["duration", "count"];

export interface StateStatistic {
  statisticId: string;
  token: string;
  label: string;
  metric: Metric;
}

// The integration slugifies with python-slugify. Entity IDs are already
// [a-z0-9_.], so lower-casing and collapsing runs of anything else is the
// same answer for them; states can hold anything, and only their token
// form is compared, so an approximation that agrees on ASCII is enough.
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

export function statisticsForEntity(
  entityId: string,
  metric: Metric,
  metadata: StatisticsMetaData[],
  filter?: { states?: string[]; ignore_states?: string[] }
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
  const ignored = new Set((filter.ignore_states ?? []).map(stateToken));
  const kept = found.filter((s) => !ignored.has(s.token));
  const byToken = new Map(kept.map((s) => [s.token, s]));
  const listed = (filter.states ?? [])
    .map((state) => byToken.get(stateToken(state)))
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
