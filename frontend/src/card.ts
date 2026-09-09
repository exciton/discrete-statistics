import { LitElement, html, css, nothing } from "lit";
import { property, state } from "lit/decorators.js";
import { ensureChartBase } from "./chart-base";
import { fetchStatistics, listStatisticIds, subscribeEnergyRange } from "./hass-api";
import { rangeFromDays, resolvePeriod, resolveUnit, type Range } from "./period";
import {
  buildSeries,
  earliestStart,
  unitLabel,
  type ChartSeries,
  type LegendItem,
} from "./series";
import { statisticsForEntity, type StateStatistic } from "./statistic-ids";
import type { CardConfig, HassLike, Metric } from "./types";

const DEFAULT_DAYS = 30;
// The stock statistics-graph card refreshes hourly; a dashboard left open
// otherwise freezes on the range it was rendered with.
const REFRESH_INTERVAL_MS = 60 * 60 * 1000;
const FALLBACK_COLORS = [
  "#4269d0", "#f4bd4a", "#ff725c", "#6cc5b0",
  "#a463f2", "#ff8ab7", "#9c6b4e", "#97bbf5",
];

// What echarts hands a tooltip formatter, for the bar series this card
// draws: value is the point, [bar time, value, bucket start, bucket end].
interface TooltipParam {
  seriesName?: string;
  color?: string;
  value: [number, number | null, number, number];
}

const METRIC_LABEL: Record<Metric, string> = {
  duration: "Time in State",
  count: "Transition Count",
};

export class DiscreteStatisticsCard extends LitElement {
  @property({ attribute: false }) public hass?: HassLike;

  @state() private _config?: CardConfig;

  @state() private _series: ChartSeries[] = [];

  @state() private _legend: LegendItem[] = [];

  @state() private _unit = "";

  @state() private _range?: Range;

  @state() private _error?: string;

  // ha-chart-base calls setOption whenever .options is a new object, so the
  // options are rebuilt only when what they are drawn from changes.
  @state() private _chartOptions: Record<string, unknown> = {};

  private _stats?: StateStatistic[];

  private _statsFor?: string;

  private _unsubEnergy?: () => void;

  private _fetching = false;

  private _pending = false;

  private _subscribed = false;

  private _refreshTimer?: number;

  // The earliest bucket start across the drawn series. The recorder snaps a
  // query outward to whole periods, so the first bucket begins before the
  // range and an x axis pinned to the range start would clip it.
  private _dataStart?: number;

  // The range the in-flight or last refresh is serving; a _range write of
  // the same object needs no fetch.
  private _refreshedRange?: Range;

  public static getStubConfig(): Partial<CardConfig> {
    return { metric: "duration", unit: "auto", period: "auto", days_to_show: DEFAULT_DAYS };
  }

  public static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true, selector: { entity: {} } },
        { name: "title", selector: { text: {} } },
        {
          name: "metric",
          selector: {
            select: {
              mode: "dropdown",
              options: [
                { value: "duration", label: METRIC_LABEL.duration },
                { value: "count", label: METRIC_LABEL.count },
              ],
            },
          },
        },
        {
          name: "unit",
          selector: {
            select: {
              mode: "dropdown",
              options: [
                { value: "auto", label: "Automatic" },
                { value: "h", label: "Hours" },
                { value: "d", label: "Days" },
                { value: "percent", label: "Percentage of the time" },
              ],
            },
          },
        },
        {
          name: "period",
          selector: {
            select: {
              mode: "dropdown",
              options: [
                { value: "auto", label: "Automatic" },
                { value: "hour", label: "Per hour" },
                { value: "day", label: "Per day" },
                { value: "week", label: "Per week" },
                { value: "month", label: "Per month" },
                { value: "year", label: "Per year" },
              ],
            },
          },
        },
        { name: "days_to_show", selector: { number: { min: 1, mode: "box" } } },
        { name: "energy_date_selection", selector: { boolean: {} } },
        { name: "hide_legend", selector: { boolean: {} } },
      ],
      computeLabel: (schema: { name: string }) =>
        ({
          entity: "Entity",
          title: "Title",
          metric: "Show",
          unit: "Time unit",
          period: "Bars",
          days_to_show: "Days to show",
          energy_date_selection: "Follow the dashboard's date picker",
          hide_legend: "Hide the legend",
        })[schema.name] ?? schema.name,
      computeHelper: (schema: { name: string }) =>
        ({
          unit: "Only for time in state. Automatic picks hours or days to suit the bars.",
          days_to_show: "Ignored when following the date picker.",
        })[schema.name],
    };
  }

  public setConfig(config: CardConfig): void {
    if (!config.entity) {
      throw new Error("entity is required");
    }
    this._config = config;
    this._stats = undefined;
    this._statsFor = undefined;
    this._dataStart = undefined;
    this._error = undefined;
    this._subscribed = false;
    this._chartOptions = this._options();
  }

  public getGridOptions() {
    return { columns: 12, min_columns: 6, min_rows: 4, rows: 6 };
  }

  public getCardSize(): number {
    return 5;
  }

  public disconnectedCallback(): void {
    super.disconnectedCallback();
    this._unsubEnergy?.();
    this._unsubEnergy = undefined;
    this._stopTicking();
    this._subscribed = false;
  }

  public connectedCallback(): void {
    super.connectedCallback();
    // Before the first update there is one pending, and its _config branch
    // subscribes; this handles re-attach.
    if (this.hasUpdated && this.hass && this._config && !this._subscribed) {
      this._subscribeRange();
    }
  }

  protected updated(changed: Map<string, unknown>): void {
    if (!this.hass || !this._config) {
      return;
    }
    if (changed.has("_config")) {
      this._subscribeRange();
    } else if (changed.has("hass") && !this._subscribed) {
      this._subscribeRange();
    }
    if (
      changed.has("_config") ||
      (changed.has("_range") && this._range !== this._refreshedRange)
    ) {
      void this._refresh();
    }
  }

  private _subscribeRange(): void {
    this._unsubEnergy?.();
    this._unsubEnergy = undefined;
    this._stopTicking();
    if (this._config?.energy_date_selection) {
      this._unsubEnergy = subscribeEnergyRange(
        this.hass!,
        this._config.collection_key,
        (range) => {
          this._range = range;
        },
        () => {
          this._error = "No energy date picker found on this dashboard";
        }
      );
    } else {
      const days = this._config?.days_to_show ?? DEFAULT_DAYS;
      this._range = rangeFromDays(days, new Date());
      // A fresh range object is all it takes: updated()'s
      // _range !== _refreshedRange guard turns it into one refresh. Dropping
      // _statsFor with it lets a state first recorded since the last run
      // gain its series.
      this._refreshTimer = window.setInterval(() => {
        this._statsFor = undefined;
        this._range = rangeFromDays(days, new Date());
      }, REFRESH_INTERVAL_MS);
    }
    this._subscribed = true;
  }

  private _stopTicking(): void {
    if (this._refreshTimer !== undefined) {
      window.clearInterval(this._refreshTimer);
      this._refreshTimer = undefined;
    }
  }

  private async _refresh(): Promise<void> {
    const hass = this.hass;
    const config = this._config;
    const range = this._range;
    if (!hass || !config || !range) {
      return;
    }
    if (this._fetching) {
      // The queued re-run will read whatever is current when it starts, so
      // this range is already accounted for.
      this._pending = true;
      this._refreshedRange = range;
      return;
    }
    this._fetching = true;
    this._refreshedRange = range;
    try {
      await ensureChartBase();
      const metric = config.metric ?? "duration";
      const key = [
        config.entity,
        metric,
        (config.states ?? []).join(","),
        (config.ignore_states ?? []).join(","),
      ].join("|");
      if (this._statsFor !== key) {
        const metadata = await listStatisticIds(hass);
        this._stats = statisticsForEntity(config.entity, metric, metadata, {
          states: config.states,
          ignore_states: config.ignore_states,
        });
        this._statsFor = key;
      }
      const stats = this._stats ?? [];
      if (!stats.length) {
        this._error = `No statistics recorded for ${config.entity}`;
        this._series = [];
        this._legend = [];
        this._dataStart = undefined;
        this._chartOptions = this._options();
        return;
      }
      const period = resolvePeriod(config.period, range);
      const unit = resolveUnit(config.unit, metric, period);
      const data = await fetchStatistics(hass, stats.map((s) => s.statisticId), range, period);
      const { series, legend } = buildSeries(config.entity, stats, data, unit, this._colors());
      this._series = series;
      this._legend = legend;
      this._dataStart = earliestStart(series);
      this._unit = unitLabel(unit);
      this._error = undefined;
      this._chartOptions = this._options();
    } catch (err) {
      this._error = err instanceof Error ? err.message : String(err);
    } finally {
      this._fetching = false;
      if (this._pending) {
        this._pending = false;
        void this._refresh();
      }
    }
  }

  // ha-chart-base renders whatever a tooltip formatter returns as lit
  // (lit-tooltip-formatter.ts), and suppresses the tooltip on `nothing`.
  // The bar's own colour is the translucent fill; the marker matches the
  // legend swatch, which is the solid one.
  private _solidColor(row: TooltipParam): string {
    return (
      this._legend.find((item) => item.name === row.seriesName)?.itemStyle
        .color ??
      row.color ??
      ""
    );
  }

  private _tooltipFormatter = (params: TooltipParam[] | TooltipParam) =>
    this._tooltip(params);

  private _tooltip(params: TooltipParam[] | TooltipParam) {
    const rows = Array.isArray(params) ? params : [params];
    if (!rows.length) {
      return nothing;
    }
    const point = rows.find((row) => row.value?.length >= 4);
    if (!point) {
      return nothing;
    }
    // A series with no value in this bucket has no bar to describe.
    const shown = rows.filter((row) => row.value?.[1] !== null);
    const [start, end] = [point.value[2], point.value[3]];
    // The tooltip is rendered outside this card's shadow root, so the
    // marker is styled inline rather than from the card's stylesheet, and
    // it is a span rather than <ha-chart-tooltip-marker>: that element
    // belongs to the frontend's chart chunk and need not be registered
    // wherever ha-chart-base is.
    return html`${this._formatSpan(start, end)}<br />${shown.map(
      (row, i) =>
        html`<span
            style="display:inline-block;width:10px;height:10px;border-radius:10px;
                   vertical-align:middle;margin-inline-end:4px;
                   background-color:${this._solidColor(row)}"
          ></span>
          ${row.seriesName}: ${this._formatValue(row.value[1]!)}${i <
          shown.length - 1
            ? html`<br />`
            : nothing}`
    )}`;
  }

  // The frontend's own date-time helpers live inside its bundle and are not
  // reachable from a separately built card, so the language from hass.locale
  // is what the card can honour.
  private _formatSpan(start: number, end: number): string {
    const language = this.hass?.locale?.language;
    const at = (ms: number) => new Date(ms).toLocaleString(language);
    return `${at(start)} – ${at(end)}`;
  }

  private _formatValue(value: number): string {
    const language = this.hass?.locale?.language;
    const shown = value.toLocaleString(language, { maximumFractionDigits: 2 });
    if (!this._unit) {
      return shown;
    }
    return this._unit === "%" ? `${shown}%` : `${shown} ${this._unit}`;
  }

  private _colors(): string[] {
    const style = getComputedStyle(this);
    const colors: string[] = [];
    for (let i = 1; i <= 8; i++) {
      const c = style.getPropertyValue(`--graph-color-${i}`).trim();
      if (c) {
        colors.push(c);
      }
    }
    return colors.length ? colors : FALLBACK_COLORS;
  }

  private _options() {
    return {
      xAxis: {
        type: "time",
        min: this._dataStart ?? this._range?.start.getTime(),
        max: this._range?.end.getTime(),
      },
      yAxis: {
        type: "value",
        name: this._unit,
        nameGap: 2,
        nameTextStyle: { align: "left" },
        // null, not undefined: ha-chart-base merges its options into the
        // chart, and echarts ignores an undefined value on merge, so the cap
        // would survive a switch away from percent.
        max: this._unit === "%" ? 100 : null,
        splitLine: { show: true },
      },
      legend: {
        type: "custom",
        show: !this._config?.hide_legend,
        data: this._legend,
      },
      grid: { top: 15, bottom: 0, left: 1, right: 1, containLabel: true },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: this._tooltipFormatter,
      },
    };
  }

  protected render() {
    if (!this._config) {
      return nothing;
    }
    // In a sections view with a fixed row count the chart fills the card
    // and the legend shares that height; otherwise the chart picks its own
    // height and the legend flows below it, as the stock statistics card
    // does.
    const fixedHeight = typeof this._config.grid_options?.rows === "number";
    return html`<ha-card
      .header=${this._config.title ?? ""}
      class=${fixedHeight ? "fixed-height" : ""}
    >
      <div class="content ${this._config.title ? "has-header" : ""}">
        ${this._error
          ? html`<div class="error">${this._error}</div>`
          : html`<ha-chart-base
              .hass=${this.hass}
              .data=${this._series}
              .options=${this._chartOptions}
              .height=${fixedHeight ? "100%" : undefined}
            ></ha-chart-base>`}
      </div>
    </ha-card>`;
  }

  static styles = css`
    ha-card {
      display: flex;
      flex-direction: column;
      height: 100%;
    }
    .content {
      padding: 16px;
      flex: 1;
    }
    .has-header {
      padding-top: 0;
    }
    ha-chart-base {
      height: 100%;
    }
    .fixed-height {
      --chart-max-height: 100%;
    }
    .error {
      color: var(--error-color);
    }
  `;
}
