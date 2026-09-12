// <ha-chart-base> is registered by the frontend's more-info dialog chunk,
// which loads shortly after first render; creating a stock
// statistics-graph card pulls that chunk in for a dashboard that gets
// here sooner.
const CHART_BASE_TIMEOUT_MS = 10_000;

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
