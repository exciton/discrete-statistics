import { LitElement, html, nothing } from "lit";
import { property, state } from "lit/decorators.js";
import { listStatisticIds } from "./hass-api";
import { entitiesWithStatistics } from "./statistic-ids";
import type { CardConfig, HassLike, Metric } from "./types";

const METRIC_LABEL: Record<Metric, string> = {
  duration: "Time in State",
  count: "Transition Count",
};

// The editor's form. The entity picker is limited to the entities the
// integration has recorded statistics for when that list is known; with
// no list — the lookup failed — it offers every entity rather than none.
export function configSchema(entities?: string[]) {
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
      name: "metric",
      selector: dropdown([
        { value: "duration", label: METRIC_LABEL.duration },
        { value: "count", label: METRIC_LABEL.count },
      ]),
    },
    {
      name: "unit",
      selector: dropdown([
        { value: "auto", label: "Automatic" },
        { value: "h", label: "Hours" },
        { value: "d", label: "Days" },
        { value: "percent", label: "Percentage of the time" },
      ]),
    },
    {
      name: "period",
      selector: dropdown([
        { value: "auto", label: "Automatic" },
        { value: "hour", label: "Per hour" },
        { value: "day", label: "Per day" },
        { value: "week", label: "Per week" },
        { value: "month", label: "Per month" },
        { value: "year", label: "Per year" },
      ]),
    },
    { name: "days_to_show", selector: { number: { min: 1, mode: "box" } } },
    { name: "energy_date_selection", selector: { boolean: {} } },
    { name: "hide_legend", selector: { boolean: {} } },
  ];
}

export const computeLabel = (schema: { name: string }) =>
  ({
    entity: "Entity",
    title: "Title",
    metric: "Show",
    unit: "Time unit",
    period: "Bars",
    days_to_show: "Days to show",
    energy_date_selection: "Follow the dashboard's date picker",
    hide_legend: "Hide the legend",
  })[schema.name] ?? schema.name;

export const computeHelper = (schema: { name: string }) =>
  ({
    unit: "Only for time in state. Automatic picks hours or days to suit the bars.",
    days_to_show: "Ignored when following the date picker.",
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
    return html`<ha-form
      .hass=${this.hass}
      .data=${this._config}
      .schema=${configSchema(this._entities ?? undefined)}
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
