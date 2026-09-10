import { describe, expect, it } from "vitest";
import { paletteCss, resolveColor, toHex } from "../src/colors";

const theme = (name: string) =>
  ({ "--red-color": "#f44336", "--light-blue-color": "#03a9f4" })[name] ?? "";

describe("toHex", () => {
  it("expands short hex and drops an alpha channel", () => {
    expect(toHex("#abc")).toBe("#aabbcc");
    expect(toHex("#abcd")).toBe("#aabbcc");
    expect(toHex("#AABBCC")).toBe("#aabbcc");
    expect(toHex("#aabbcc80")).toBe("#aabbcc");
  });

  it("reads rgb()", () => {
    expect(toHex("rgb(255, 0, 16)")).toBe("#ff0010");
    expect(toHex("rgba(0,0,0,0.5)")).toBe("#000000");
  });

  it("is undefined for anything else", () => {
    expect(toHex("tomato")).toBeUndefined();
    expect(toHex("#abcde")).toBeUndefined();
    expect(toHex("")).toBeUndefined();
  });
});

describe("resolveColor", () => {
  it("resolves a theme name through its variable", () => {
    expect(resolveColor("red", theme)).toBe("#f44336");
    expect(resolveColor("Light-Blue", theme)).toBe("#03a9f4");
  });

  it("takes hex as written and passes on anything it cannot draw", () => {
    expect(resolveColor("#ABC", theme)).toBe("#aabbcc");
    expect(resolveColor("grey", theme)).toBeUndefined();
    expect(resolveColor("tomato", theme)).toBeUndefined();
    expect(resolveColor(undefined, theme)).toBeUndefined();
  });
});

describe("paletteCss", () => {
  it("is the theme's graph colour for the position, with the card's fallback behind it", () => {
    expect(paletteCss(0)).toBe("var(--graph-color-1, #4269d0)");
    expect(paletteCss(3)).toBe("var(--graph-color-4, #6cc5b0)");
  });

  it("wraps as the card's palette does", () => {
    expect(paletteCss(8)).toBe(paletteCss(0));
    expect(paletteCss(9)).toBe(paletteCss(1));
  });
});
