import { DiscreteStatisticsCard } from "./card";

customElements.define("discrete-statistics-card", DiscreteStatisticsCard);

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
