// Theme token renderer (ADR 0012). Input is the published document from GET /api/v1/theme.
// Every key and value is re-validated here so theme data can never inject CSS, even if the API
// contract were bypassed.
import type { paths } from "@ecomm/api-contract";

export type PublishedTheme =
  paths["/api/v1/theme"]["get"]["responses"]["200"]["content"]["application/json"];
export type ThemeDocument = PublishedTheme["document"];

const KEY = /^[a-z-]+$/;
const RGB = /^(25[0-5]|2[0-4]\d|1?\d?\d) (25[0-5]|2[0-4]\d|1?\d?\d) (25[0-5]|2[0-4]\d|1?\d?\d)$/;
const RADII = new Set(["0rem", "0.25rem", "0.5rem", "0.75rem", "1rem", "9999px"]);

export const FONT_STACKS: Record<string, string> = {
  "inter-hind-siliguri": '"Inter", "Hind Siliguri", ui-sans-serif, system-ui, sans-serif',
  "poppins-noto-sans-bengali": '"Poppins", "Noto Sans Bengali", ui-sans-serif, system-ui, sans-serif',
  "lora-noto-serif-bengali": '"Lora", "Noto Serif Bengali", ui-serif, Georgia, serif',
  "roboto-baloo-da-2": '"Roboto", "Baloo Da 2", ui-sans-serif, system-ui, sans-serif',
  system: 'ui-sans-serif, system-ui, "Noto Sans Bengali", sans-serif',
};

type Palette = Record<string, string>;

function vars(p: Palette): string {
  return Object.entries(p)
    .map(([k, v]) => [k.replace(/_/g, "-"), v] as const)
    .filter(([k, v]) => KEY.test(k) && typeof v === "string" && RGB.test(v))
    .map(([k, v]) => `--color-${k}:${v};`)
    .join("");
}

export function themeToCss(doc: Pick<ThemeDocument, "tokens">): string {
  const t = doc.tokens as unknown as {
    colors: Palette; dark: Palette; radius?: string; font_body?: string; font_heading?: string;
  };
  const radius = t.radius && RADII.has(t.radius) ? `--radius:${t.radius};` : "";
  const body = FONT_STACKS[t.font_body ?? ""] ?? FONT_STACKS.system;
  const heading = FONT_STACKS[t.font_heading ?? ""] ?? body;
  const fonts = `--font-body:${body};--font-heading:${heading};`;
  const dark = vars(t.dark ?? {});
  return (
    `:root{${vars(t.colors)}${radius}${fonts}}` +
    `[data-theme="dark"]{${dark}}` +
    `@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){${dark}}}`
  );
}

/** Back-compat helper used by Phase 1 tests. */
export function tokensToCss(t: { colors: Palette; dark?: Palette; radius?: string }): string {
  return themeToCss({ tokens: { dark: {}, ...t } as never });
}
