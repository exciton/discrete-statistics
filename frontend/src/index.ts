import { DiscreteStatisticsCard } from "./card";
import { DiscreteStatisticsCardEditor } from "./editor";
import { DiscreteStatisticsStateList } from "./state-list-element";

// The frontend's app bundle replaces window.customElements with the
// scoped-custom-element-registry polyfill, which knows only what was
// defined through it, and this module is imported in parallel with that
// bundle — so a definition made before the swap is invisible to the
// dashboard. <home-assistant> is defined after the swap and whenDefined()
// resolves on either registry, so defining then lands on the registry the
// app consults.
customElements.whenDefined("home-assistant").then(() => {
  customElements.define("discrete-statistics-card", DiscreteStatisticsCard);
  customElements.define(
    "discrete-statistics-card-editor",
    DiscreteStatisticsCardEditor
  );
  customElements.define(
    "discrete-statistics-state-list",
    DiscreteStatisticsStateList
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
