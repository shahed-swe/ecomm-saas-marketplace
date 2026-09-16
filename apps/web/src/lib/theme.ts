// Theme token contract (ADR 0012). Phase 4 loads a tenant's published theme;
// Phase 1 ships the renderer so components never depend on raw colors.
export type Rgb = `${number} ${number} ${number}`;

export interface ThemeTokens {
  colors: Record<
    "primary" | "primary-fg" | "secondary" | "accent" | "bg" | "surface" | "fg" | "muted" | "border" | "danger" | "success",
    Rgb
  >;
  dark?: Partial<ThemeTokens["colors"]>;
  radius?: string;
}

const KEY = /^[a-z-]+$/;
const RGB = /^\d{1,3} \d{1,3} \d{1,3}$/;
const RADIUS = /^\d+(\.\d+)?(rem|px)$/;

/** Serialise tokens to CSS. Validates every key/value so theme data can never inject CSS. */
export function tokensToCss(t: ThemeTokens): string {
  const vars = (c: Partial<ThemeTokens["colors"]>) =>
    Object.entries(c)
      .filter(([k, v]) => KEY.test(k) && typeof v === "string" && RGB.test(v))
      .map(([k, v]) => `--color-${k}:${v};`)
      .join("");
  const radius = t.radius && RADIUS.test(t.radius) ? `--radius:${t.radius};` : "";
  const dark = t.dark ? `[data-theme="dark"]{${vars(t.dark)}}` : "";
  return `:root{${vars(t.colors)}${radius}}${dark}`;
}
