import { LitElement, css, html, nothing } from "lit";
import { property, state } from "lit/decorators.js";
import { listStatisticIds } from "./hass-api";
import { applyChartMode, isMultiEntity, type ChartMode } from "./config";
import {
  seriesList,
  seriesListConfig,
  stateList,
  stateListConfig,
  type StateList,
} from "./state-list";
import { entitiesWithStatistics, statisticsForEntity } from "./statistic-ids";
import type {
  CardConfig,
  ChartType,
  HassLike,
  Metric,
  Period,
  StatisticsMetaData,
} from "./types";

const METRIC_LABEL: Record<Metric, string> = {
  duration: "Time in State",
  count: "Transition Count",
};

const CHART_TYPE_LABEL: Record<ChartType, string> = {
  line: "Line",
  "line-stack": "Stacked line",
  bar: "Bar",
  "bar-stack": "Stacked bar",
};

const PERIOD_LABEL: Record<Period, string> = {
  auto: "Auto",
  hour: "Hour",
  day: "Day",
  week: "Week",
  month: "Month",
  year: "Year",
};

// Laid out as the stock statistics-graph card's form is. The entity picker
// is limited to the entities the integration has statistics for when that
// list is known; with no list — the lookup failed — it offers every entity
// rather than none.
export function configSchema(
  entities?: string[],
  followsPicker = false,
  multi = false
) {
  const dropdown = (options: { value: string; label: string }[]) => ({
    select: { mode: "dropdown", options },
  });
  return [
    {
      name: "chart_mode",
      required: true,
      selector: {
        select: {
          mode: "list",
          options: [
            { value: "states", label: "States of one entity" },
            { value: "entities", label: "One state from several" },
          ],
        },
      },
    },
    ...(multi
      ? []
      : [
          {
            name: "entity",
            required: true,
            selector: { entity: entities ? { include_entities: entities } : {} },
          },
        ]),
    { name: "title", selector: { text: {} } },
    {
      name: "",
      type: "grid",
      schema: [
        {
          name: "",
          type: "grid",
          schema: [
            {
              name: "chart_type",
              required: true,
              selector: {
                select: {
                  mode: "list",
                  options: (Object.keys(CHART_TYPE_LABEL) as ChartType[]).map(
                    (value) => ({ value, label: CHART_TYPE_LABEL[value] })
                  ),
                },
              },
            },
            followsPicker
              ? { name: "collection_key", selector: { text: {} } }
              : {
                  name: "days_to_show",
                  selector: { number: { min: 1, mode: "box" } },
                },
          ],
        },
        {
          name: "period",
          required: true,
          selector: {
            select: {
              mode: "list",
              options: (Object.keys(PERIOD_LABEL) as Period[]).map((value) => ({
                value,
                label: PERIOD_LABEL[value],
              })),
            },
          },
        },
      ],
    },
    {
      name: "",
      type: "grid",
      schema: [
        {
          name: "metric",
          required: true,
          selector: dropdown([
            { value: "duration", label: METRIC_LABEL.duration },
            { value: "count", label: METRIC_LABEL.count },
          ]),
        },
        {
          name: "unit",
          required: true,
          // A count has no time unit; the field goes with it.
          visible: { field: "metric", operator: "not_eq", value: "count" },
          selector: dropdown([
            { value: "auto", label: "Automatic" },
            { value: "h", label: "Hours" },
            { value: "d", label: "Days" },
            { value: "percent", label: "Percentage of the time" },
          ]),
        },
      ],
    },
    {
      name: "",
      type: "grid",
      schema: [
        { name: "energy_date_selection", selector: { boolean: {} } },
        { name: "hide_legend", selector: { boolean: {} } },
      ],
    },
  ];
}

export const computeLabel = (schema: { name: string }) =>
  ({
    chart_mode: "Chart",
    entity: "Entity",
    title: "Title",
    chart_type: "Chart type",
    period: "Period",
    days_to_show: "Days to show",
    collection_key: "Collection key",
    energy_date_selection: "Follow the date picker",
    metric: "Show",
    unit: "Time unit",
    hide_legend: "Hide the legend",
  })[schema.name] ?? schema.name;

export const computeHelper = (schema: { name: string }) =>
  ({
    chart_mode:
      "Whether the chart's series are one entity's states, or one state from each of several entities.",
    collection_key: "Names the date picker when a dashboard has more than one.",
  })[schema.name];

export class DiscreteStatisticsCardEditor extends LitElement {
  @property({ attribute: false }) public hass?: HassLike;

  @state() private _config?: CardConfig;

  // The user's last explicit choice, which matters only while the config
  // can express neither mode.
  @state() private _mode?: ChartMode;

  // undefined until the lookup answers; null when it failed.
  @state() private _entities?: string[] | null;

  @state() private _metadata: StatisticsMetaData[] = [];

  private _loading = false;

  public setConfig(config: CardConfig): void {
    this._config = config;
    if (config.entity || config.states?.length) {
      this._mode = undefined;
    }
  }

  protected willUpdate() {
    if (this.hass && this._entities === undefined && !this._loading) {
      this._loading = true;
      this._load(this.hass);
    }
  }

  private async _load(hass: HassLike) {
    try {
      const metadata = await listStatisticIds(hass);
      this._metadata = metadata;
      this._entities = entitiesWithStatistics(
        Object.keys(hass.states ?? {}),
        metadata
      );
    } catch {
      this._entities = null;
    }
  }

  protected render() {
    if (!this._config || this._entities === undefined) {
      return nothing;
    }
    const mode = this._mode ?? (isMultiEntity(this._config) ? "entities" : "states");
    const multi = mode === "entities";
    // A config missing these keys shows the card's defaults rather than
    // blank fields.
    const data = {
      ...this._config,
      chart_mode: mode,
      chart_type: this._config.chart_type ?? "bar-stack",
      period: this._config.period ?? "auto",
      metric: this._config.metric ?? "duration",
      unit: this._config.unit ?? "auto",
    };
    const list = multi
      ? seriesList(this._config.states ?? [], data.metric, this._metadata)
      : stateList(
          statisticsForEntity(this._config.entity ?? "", data.metric, this._metadata),
          this._config
        );
    const stateOptions = Object.fromEntries(
      list.rows
        .map((row) => row.entity)
        .filter((entity): entity is string => !!entity)
        .map((entity) => [
          entity,
          statisticsForEntity(entity, data.metric, this._metadata).map((s) => ({
            value: s.token,
            label: s.label,
          })),
        ])
    );
    return html`<ha-form
        .hass=${this.hass}
        .data=${data}
        .schema=${configSchema(
          this._entities ?? undefined,
          !!this._config.energy_date_selection,
          multi
        )}
        .computeLabel=${computeLabel}
        .computeHelper=${computeHelper}
        @value-changed=${this._valueChanged}
      ></ha-form>
      ${multi || list.rows.length
        ? html`<div class="states">
            <div class="heading">${multi ? "Series" : "States"}</div>
            <discrete-statistics-state-list
              .hass=${this.hass}
              .value=${list}
              .entities=${this._entities ?? undefined}
              .stateOptions=${stateOptions}
              @value-changed=${this._statesChanged}
            ></discrete-statistics-state-list>
          </div>`
        : nothing}`;
  }

  private _statesChanged(ev: CustomEvent<{ value: StateList }>): void {
    ev.stopPropagation();
    const { states: _states, ignore_states: _ignored, ...rest } = this._config!;
    const config = isMultiEntity(this._config!)
      ? seriesListConfig(ev.detail.value)
      : stateListConfig(ev.detail.value);
    this._announce({ ...rest, ...config });
  }

  private _valueChanged(ev: CustomEvent): void {
    ev.stopPropagation();
    const { chart_mode: mode, ...config } = ev.detail.value;
    this._mode = mode;
    this._announce(
      applyChartMode(config, mode, config.metric ?? "duration", this._metadata)
    );
  }

  private _announce(config: CardConfig): void {
    this._config = config;
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config },
        bubbles: true,
        composed: true,
      })
    );
  }

  static styles = css`
    .states {
      margin-top: 24px;
    }
    .heading {
      font-size: 16px;
      font-weight: 500;
      margin-bottom: 8px;
    }
  `;
}
