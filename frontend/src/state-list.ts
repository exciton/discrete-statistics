// The editor's state list, and the config it stands for.
//
// `states:` orders the states drawn and carries their colours;
// `ignore_states:` hides. `states:` alone is a closed list — a state the
// entity gains later is not drawn — and `ignore_states:` opens it. One row
// per state, ticked when drawn, and one "ignore new states" tick that
// decides where the unticked rows go: left out of `states:`, or listed in
// `ignore_states:` — empty when every row is ticked, since the key's
// presence is what opens the list.
import { settingMatches, type StateStatistic } from "./statistic-ids";
import type { StateSetting } from "./types";

export interface StateRow {
  token: string;
  // The stored name; `name` is the configured one drawn in its place.
  label: string;
  shown: boolean;
  name?: string;
  color?: string;
}

export interface StateList {
  rows: StateRow[];
  ignoreNew: boolean;
}

export interface StateFilter {
  states?: StateSetting[];
  ignore_states?: string[];
}

// Listed states first in their order, the rest by label, as the card
// draws them.
export function stateList(all: StateStatistic[], filter: StateFilter): StateList {
  const ignoreNew = !!filter.states && !filter.ignore_states;
  const ignored = (s: StateStatistic) =>
    (filter.ignore_states ?? []).some((entry) => settingMatches(entry, s));
  const listed: StateRow[] = [];
  const seen = new Set<StateStatistic>();
  for (const setting of filter.states ?? []) {
    const s = all.find((candidate) => settingMatches(setting, candidate));
    if (!s || seen.has(s)) {
      continue;
    }
    seen.add(s);
    const row: StateRow = { token: s.token, label: s.label, shown: !ignored(s) };
    if (typeof setting !== "string") {
      if (setting.name) {
        row.name = setting.name;
      }
      if (setting.color) {
        row.color = setting.color;
      }
    }
    listed.push(row);
  }
  const rest = all
    .filter((s) => !seen.has(s))
    .sort((a, b) => a.label.localeCompare(b.label))
    .map((s) => ({ token: s.token, label: s.label, shown: !ignoreNew && !ignored(s) }));
  return { rows: [...listed, ...rest], ignoreNew };
}

// The name a row is written under: its token, or its label for a state
// whose text has no token.
const nameOf = (row: StateRow): string => row.token || row.label;

export function stateListConfig(list: StateList): StateFilter {
  const states: StateSetting[] = list.rows
    .filter((row) => row.shown)
    .map((row) =>
      row.name || row.color
        ? {
            state: nameOf(row),
            ...(row.name ? { name: row.name } : {}),
            ...(row.color ? { color: row.color } : {}),
          }
        : nameOf(row)
    );
  if (list.ignoreNew) {
    return { states };
  }
  return {
    states,
    ignore_states: list.rows.filter((row) => !row.shown).map(nameOf),
  };
}

// The palette position the chart would give a row: it hands out colours in
// order over the states it draws, so an undrawn row shows the place it
// would take if it were ticked.
export function automaticIndex(rows: StateRow[], index: number): number {
  return rows.slice(0, index).filter((row) => row.shown).length;
}
