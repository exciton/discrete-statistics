import { LitElement, html, nothing } from "lit";
import { property, state } from "lit/decorators.js";
import { listStatisticIds } from "./hass-api";
import { entitiesWithStatistics } from "./statistic-ids";
import type { CardConfig, ChartType, HassLike, Metric, Period } from "./types";

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

// The editor's form, laid out as the stock statistics-graph card's is:
// chart type over days to show beside the period list, then the date
// picker, then what is this card's own. The entity picker is limited to
// the entities the integration has recorded statistics for when that list
// is known; with no list — the lookup failed — it offers every entity
// rather than none. Days to show gives way to the collection key while
// the card follows the date picker.
export function configSchema(entities?: string[], followsPicker = false) {
  const dropdown = (options: { value: string; label: string }[]) => ({
    select: { mode: "dropdown", options },
  });
  return [
    {
      name: "entity",
      required: true,
      selector: { entity: entities ? { include_entities: entities } : {} },
    },
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
    { name: "energy_date_selection", selector: { boolean: {} } },
    {
      name: "",
      type: "grid",
      schema: [
        {
          name: "metric",
          selector: dropdown([
            { value: "duration", label: METRIC_LABEL.duration },
            { value: "count", label: METRIC_LABEL.count },
          ]),
        },
        {
          name: "unit",
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
    { name: "hide_legend", selector: { boolean: {} } },
  ];
}

export const computeLabel = (schema: { name: string }) =>
  ({
    entity: "Entity",
    title: "Title",
    chart_type: "Chart type",
    period: "Period",
    days_to_show: "Days to show",
    collection_key: "Collection key",
    energy_date_selection: "Follow the dashboard's date picker",
    metric: "Show",
    unit: "Time unit",
    hide_legend: "Hide the legend",
  })[schema.name] ?? schema.name;

export const computeHelper = (schema: { name: string }) =>
  ({
    period: "Auto suits the period to the days shown.",
    collection_key: "Names the date picker when a dashboard has more than one.",
    unit: "Automatic picks hours or days to suit the period.",
  })[schema.name];

export class DiscreteStatisticsCardEditor extends LitElement {
  @property({ attribute: false }) public hass?: HassLike;

  @state() private _config?: CardConfig;

  // undefined until the lookup answers; null when it failed.
  @state() private _entities?: string[] | null;

  private _loading = false;

  public setConfig(config: CardConfig): void {
    this._config = config;
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
    // A config written before these keys existed, or by hand, shows the
    // card's defaults rather than blank fields.
    const data = { chart_type: "bar-stack", period: "auto", ...this._config };
    return html`<ha-form
      .hass=${this.hass}
      .data=${data}
      .schema=${configSchema(
        this._entities ?? undefined,
        !!this._config.energy_date_selection
      )}
      .computeLabel=${computeLabel}
      .computeHelper=${computeHelper}
      @value-changed=${this._valueChanged}
    ></ha-form>`;
  }

  private _valueChanged(ev: CustomEvent): void {
    ev.stopPropagation();
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: ev.detail.value },
        bubbles: true,
        composed: true,
      })
    );
  }
}
