// A configured colour, as the card draws it.
//
// The editor's colour picker hands back the frontend's theme colour
// names, resolved here through the CSS variable each one stands for
// (`red` is `--red-color`), as the stock statistics-graph card does. Hex
// is taken as written. Anything else — a named web colour, an unset
// variable — is not a colour the card can draw with, so it falls back to
// the palette rather than passing a value echarts would reject.

// The names the frontend's ui_color selector offers (compute-color.ts).
export const THEME_COLORS = [
  "primary", "accent", "red", "pink", "purple", "deep-purple", "indigo",
  "blue", "light-blue", "cyan", "teal", "green", "light-green", "lime",
  "yellow", "amber", "orange", "deep-orange", "brown", "light-grey",
  "grey", "dark-grey", "blue-grey", "black", "white",
] as const;

export type CssVariable = (name: string) => string;

// Six-digit hex, or undefined for what is not hex; a short form is
// expanded and an alpha channel dropped, since the chart adds its own.
export function toHex(color: string): string | undefined {
  const trimmed = color.trim().toLowerCase();
  const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])[0-9a-f]?$/.exec(trimmed);
  if (short) {
    const [, r, g, b] = short;
    return `#${r}${r}${g}${g}${b}${b}`;
  }
  const long = /^#([0-9a-f]{6})(?:[0-9a-f]{2})?$/.exec(trimmed);
  if (long) {
    return `#${long[1]}`;
  }
  const rgb = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(trimmed);
  if (rgb) {
    const channel = (n: string) =>
      Math.min(255, Number(n)).toString(16).padStart(2, "0");
    return `#${channel(rgb[1])}${channel(rgb[2])}${channel(rgb[3])}`;
  }
  return undefined;
}

export function resolveColor(
  color: string | undefined,
  cssVariable: CssVariable
): string | undefined {
  if (!color) {
    return undefined;
  }
  const name = color.trim().toLowerCase();
  if ((THEME_COLORS as readonly string[]).includes(name)) {
    return toHex(cssVariable(`--${name}-color`));
  }
  return toHex(color);
}

// The chart's palette: the theme's `--graph-color-1` to `-8`, and these
// when a theme sets none. The card reads the variables; `paletteCss`
// is the same choice written as CSS, for the editor to show a row the
// colour the chart will give it.
export const PALETTE_SIZE = 8;

export const FALLBACK_COLORS = [
  "#4269d0", "#f4bd4a", "#ff725c", "#6cc5b0",
  "#a463f2", "#ff8ab7", "#9c6b4e", "#97bbf5",
];

export function paletteCss(index: number): string {
  const slot = index % PALETTE_SIZE;
  return `var(--graph-color-${slot + 1}, ${FALLBACK_COLORS[slot]})`;
}
