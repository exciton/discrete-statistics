// <ha-chart-base> is registered by the frontend's more-info dialog chunk,
// which loads right after the app's first render. If a dashboard renders
// this card before that, creating a stock statistics-graph card pulls the
// chunk in.
export async function ensureChartBase(): Promise<void> {
  if (customElements.get("ha-chart-base")) {
    return;
  }
  const helpers = await (window as any).loadCardHelpers?.();
  helpers?.createCardElement({
    type: "statistics-graph",
    entities: ["sensor.none"],
  });
  await customElements.whenDefined("ha-chart-base");
}
