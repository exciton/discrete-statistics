import type { Range } from "./period";
import type { HassLike, ResolvedPeriod, Statistics, StatisticsMetaData } from "./types";

export const listStatisticIds = (hass: HassLike) =>
  hass.callWS<StatisticsMetaData[]>({ type: "recorder/list_statistic_ids" });

export const fetchStatistics = (
  hass: HassLike,
  ids: string[],
  range: Range,
  period: ResolvedPeriod
) =>
  hass.callWS<Statistics>({
    type: "recorder/statistics_during_period",
    start_time: range.start.toISOString(),
    end_time: range.end.toISOString(),
    statistic_ids: ids,
    period,
    types: ["change"],
  });

// The energy-date-selection card keeps its collection on the connection
// object under "_<key>"; the default key is "energy_<panel url>"
// (frontend src/data/energy.ts, convertCollectionKeyToConnection). The
// collection may not exist yet when this card first renders, so poll for
// it briefly rather than assume.
interface EnergyCollection {
  subscribe(cb: (data: { start: Date; end?: Date }) => void): () => void;
}

export function subscribeEnergyRange(
  hass: HassLike,
  collectionKey: string | undefined,
  onRange: (range: Range) => void
): () => void {
  const key = `_${collectionKey ?? `energy_${hass.panelUrl ?? ""}`}`;
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
    }
  }, 100);
  return () => {
    window.clearInterval(timer);
    unsub?.();
  };
}
