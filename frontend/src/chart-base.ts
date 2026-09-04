// <ha-chart-base> is registered by the frontend's more-info dialog chunk,
// which loads right after the app's first render. If a dashboard renders
// this card before that, creating a stock statistics-graph card pulls the
// chunk in.
export const CHART_BASE_TIMEOUT_MS = 10_000;

export async function ensureChartBase(): Promise<void> {
  if (customElements.get("ha-chart-base")) {
    return;
  }
  const helpers = await (window as any).loadCardHelpers?.();
  helpers?.createCardElement({
    type: "statistics-graph",
    entities: ["sensor.none"],
  });
  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      reject(new Error("Home Assistant's chart component did not load"));
    }, CHART_BASE_TIMEOUT_MS);
    customElements.whenDefined("ha-chart-base").then(() => {
      window.clearTimeout(timer);
      resolve();
    });
  });
}
