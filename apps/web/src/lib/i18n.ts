export type Locale = "bn" | "en";
export type Bilingual = { en?: string; bn?: string } | null | undefined;

/** Pick the tenant/buyer locale, falling back to the other language rather than showing nothing. */
export function t(text: Bilingual, locale: Locale): string {
  if (!text) return "";
  return (locale === "bn" ? text.bn || text.en : text.en || text.bn) ?? "";
}
