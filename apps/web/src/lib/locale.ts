import { cookies } from "next/headers";
import bn from "@/messages/bn.json";
import en from "@/messages/en.json";
import { getStore } from "./api";
import type { Locale } from "./i18n";

const DICTS = { en, bn } as const;
export type MessageKey = keyof typeof en;

/** Buyer choice (cookie) > tenant default. */
export async function getLocale(): Promise<Locale> {
  const c = (await cookies()).get("hl")?.value;
  if (c === "bn" || c === "en") return c;
  const store = await getStore();
  return (store?.default_locale as Locale) ?? "bn";
}

export async function getMessages() {
  const locale = await getLocale();
  const dict = DICTS[locale];
  return {
    locale,
    m: (key: MessageKey, vars: Record<string, string | number> = {}) =>
      Object.entries(vars).reduce((s, [k, v]) => s.replace(`{${k}}`, locale === "bn" ? toBnDigits(String(v)) : String(v)), dict[key]),
  };
}

export function toBnDigits(s: string) {
  return s.replace(/\d/g, (d) => "০১২৩৪৫৬৭৮৯"[+d]);
}
