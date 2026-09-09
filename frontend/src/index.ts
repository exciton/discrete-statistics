import { DiscreteStatisticsCard } from "./card";
import { DiscreteStatisticsCardEditor } from "./editor";

// The frontend's app bundle replaces window.customElements with the
// scoped-custom-element-registry polyfill, whose get() and whenDefined()
// know only what was defined through it. The shell imports this module
// in parallel with that bundle, so a definition made before the swap is
// invisible to the dashboard afterwards. <home-assistant> is defined by
// the app after the swap, and whenDefined() resolves for it on either
// registry, so defining then lands on the one the app consults.
customElements.whenDefined("home-assistant").then(() => {
  customElements.define("discrete-statistics-card", DiscreteStatisticsCard);
  customElements.define(
    "discrete-statistics-card-editor",
    DiscreteStatisticsCardEditor
  );
});

declare global {
  interface Window {
    customCards?: { type: string; name: string; description: string; preview?: boolean }[];
  }
}

window.customCards = window.customCards ?? [];
window.customCards.push({
  type: "discrete-statistics-card",
  name: "Discrete Statistics",
  description: "Time in each state, or transitions into it, per hour, day, week, month or year.",
  preview: false,
});
