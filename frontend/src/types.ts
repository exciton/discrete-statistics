export type Metric = "duration" | "count";
export type Unit = "auto" | "h" | "d" | "percent";
export type Period = "auto" | "hour" | "day" | "week" | "month" | "year";
export type ResolvedPeriod = Exclude<Period, "auto">;
// The stock statistics-graph card's four, under the same key, so a config
// ports between the two cards.
export type ChartType = "line" | "line-stack" | "bar" | "bar-stack";

// An entry of `states:`: the state alone, or with the name it is drawn
// under and the colour it draws in — a theme colour name as the stock
// card takes (`red`, `light-blue`) or a hex value.
export type StateSetting =
  | string
  | { state: string; name?: string; color?: string };

export interface CardConfig {
  type: string;
  entity: string;
  metric?: Metric;
  unit?: Unit;
  period?: Period;
  chart_type?: ChartType;
  states?: StateSetting[];
  ignore_states?: string[];
  days_to_show?: number;
  energy_date_selection?: boolean;
  collection_key?: string;
  title?: string;
  hide_legend?: boolean;
  // Set by the dashboard, not the user: present when a sections view has
  // given the card a fixed number of rows.
  grid_options?: { rows?: number | "auto"; columns?: number | "full" };
}

// A bucket as discrete_statistics/buckets answers it: its period's edges
// in ms since epoch and the change across it.
export interface StatisticValue {
  start: number;
  end: number;
  change?: number | null;
}

export type Statistics = Record<string, StatisticValue[]>;

// What recorder/list_statistic_ids returns; only the fields the card reads.
export interface StatisticsMetaData {
  statistic_id: string;
  source: string;
  name?: string | null;
  statistics_unit_of_measurement: string | null;
  has_sum: boolean;
  unit_class: string | null;
}

// The slice of the hass object the card touches.
export interface HassLike {
  callWS<T>(msg: Record<string, unknown>): Promise<T>;
  connection: Record<string, unknown>;
  states?: Record<string, unknown>;
  panelUrl?: string;
  locale: { language: string };
  localize?: (key: string, ...args: unknown[]) => string;
}
