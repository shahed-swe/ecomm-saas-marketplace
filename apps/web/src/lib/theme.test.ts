import { tokensToCss, type ThemeTokens } from "./theme";

const base: ThemeTokens = {
  colors: {
    primary: "1 2 3", "primary-fg": "255 255 255", secondary: "0 0 0", accent: "0 0 0", bg: "0 0 0",
    surface: "0 0 0", fg: "0 0 0", muted: "0 0 0", border: "0 0 0", danger: "0 0 0", success: "0 0 0",
  },
};

function assert(cond: unknown, msg: string) { if (!cond) throw new Error(msg); }

const css = tokensToCss({ ...base, radius: "4px", dark: { bg: "9 9 9" } });
assert(css.includes("--color-primary:1 2 3;"), "primary var");
assert(css.includes('[data-theme="dark"]{--color-bg:9 9 9;}'), "dark block");
const evil = tokensToCss({
  ...base,
  radius: "1px;} body{display:none",
  colors: { ...base.colors, primary: "1 2 3;}</style><script>" as never },
});
assert(!evil.includes("script") && !evil.includes("display:none"), "injection filtered");
console.log("theme.test.ts: ok");
