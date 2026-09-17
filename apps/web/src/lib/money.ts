const bn = new Intl.NumberFormat("bn-BD", { maximumFractionDigits: 2 });
const en = new Intl.NumberFormat("en-BD", { maximumFractionDigits: 2 });

/** Displays what the API computed. Never does arithmetic on prices. */
export function taka(amount: string | null | undefined, locale: "bn" | "en" = "en"): string {
  if (amount == null) return "";
  const n = Number(amount);
  return `৳${(locale === "bn" ? bn : en).format(n)}`;
}
