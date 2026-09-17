import { themeToCss, tokensToCss } from "./theme";

function assert(cond: unknown, msg: string) { if (!cond) throw new Error(msg); }

const colors = {
  primary: "1 2 3", "primary-fg": "255 255 255", secondary: "0 0 0", accent: "0 0 0", bg: "0 0 0",
  surface: "0 0 0", fg: "0 0 0", muted: "0 0 0", border: "0 0 0", danger: "0 0 0", success: "0 0 0",
};

const css = themeToCss({ tokens: { colors, dark: { ...colors, bg: "9 9 9" }, radius: "0.25rem",
  font_body: "lora-noto-serif-bengali" } as never });
assert(css.includes("--color-primary:1 2 3;"), "primary var");
assert(css.includes("--color-primary-fg:255 255 255;"), "alias key");
assert(css.includes('[data-theme="dark"]{') && css.includes("--color-bg:9 9 9;"), "dark block");
assert(css.includes("prefers-color-scheme: dark"), "system dark");
assert(css.includes("Noto Serif Bengali"), "font stack");

const evil = themeToCss({ tokens: { colors: { ...colors, primary: "1 2 3;}</style><script>" },
  dark: {}, radius: "1px;} body{display:none", font_body: "url(https://evil)" } as never });
assert(!evil.includes("script") && !evil.includes("display:none") && !evil.includes("evil"), "injection filtered");
assert(tokensToCss({ colors }).includes("--color-bg:0 0 0;"), "compat");
console.log("theme.test.ts: ok");
