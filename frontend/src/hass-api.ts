import type { Range } from "./period";
import type { HassLike, ResolvedPeriod, Statistics, StatisticsMetaData } from "./types";

export const listStatisticIds = (hass: HassLike) =>
  hass.callWS<StatisticsMetaData[]>({ type: "recorder/list_statistic_ids" });

// The integration's own command, not recorder/statistics_during_period: it
// answers from the rows at the bucket edges rather than reducing every
// hourly row server-side, and a bucket's start and end are its period's
// edges, which is what the ratio divides by.
export const fetchStatistics = (
  hass: HassLike,
  ids: string[],
  range: Range,
  period: ResolvedPeriod
) =>
  hass.callWS<Statistics>({
    type: "discrete_statistics/buckets",
    statistic_ids: ids,
    start_time: range.start.toISOString(),
    end_time: range.end.toISOString(),
    period,
  });

// The energy-date-selection card keeps its collection on the connection
// object under "_<key>"; the default key is "energy_<panel url>", or bare
// "energy" with no panel url (frontend src/data/energy.ts,
// convertCollectionKeyToConnection). It may not exist yet when this card
// first renders, hence the brief poll.
interface EnergyCollection {
  subscribe(cb: (data: { start: Date; end?: Date }) => void): () => void;
}

export function subscribeEnergyRange(
  hass: HassLike,
  collectionKey: string | undefined,
  onRange: (range: Range) => void,
  onMissing: () => void
): () => void {
  const key = collectionKey
    ? `_${collectionKey}`
    : hass.panelUrl
      ? `_energy_${hass.panelUrl}`
      : "_energy";
  let unsub: (() => void) | undefined;
  let attempts = 0;
  const timer = window.setInterval(() => {
    const collection = hass.connection[key] as EnergyCollection | undefined;
    if (collection) {
      window.clearInterval(timer);
      unsub = collection.subscribe((data) =>
        onRange({ start: data.start, end: data.end ?? new Date() })
      );
    } else if (++attempts > 50) {
      window.clearInterval(timer);
      onMissing();
    }
  }, 100);
  return () => {
    window.clearInterval(timer);
    unsub?.();
  };
}
