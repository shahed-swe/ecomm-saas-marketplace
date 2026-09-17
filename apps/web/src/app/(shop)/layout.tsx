import Link from "next/link";
import { headers } from "next/headers";
import { getPublishedTheme, getStore } from "@/lib/api";
import { t } from "@/lib/i18n";
import { getMessages } from "@/lib/locale";
import { themeToCss } from "@/lib/theme";

// Storefront shell: tenant tokens become CSS variables before first paint; header/footer come from the
// published theme layouts. Custom CSS is scoped to [data-tenant-css] and never loaded on checkout.
export default async function ShopLayout({ children }: { children: React.ReactNode }) {
  const [theme, store, h] = await Promise.all([getPublishedTheme(), getStore(), headers()]);
  const doc = theme?.document as any;
  const brand = doc?.brand;
  const { locale, m } = await getMessages();
  const header = doc?.layouts?.header ?? { menu: [], logo_position: "left" };
  const footer = doc?.layouts?.footer ?? { columns: [] };
  const path = h.get("x-invoke-path") ?? h.get("next-url") ?? "";
  const sensitive = /^\/(checkout|account\/security|payment)/.test(path);
  return (
    <div data-surface="shop" lang={locale} className="min-h-screen bg-bg text-fg">
      {theme && <style id="tenant-theme" dangerouslySetInnerHTML={{ __html: themeToCss(theme.document) }} />}
      {doc?.custom_css && !sensitive && <link rel="stylesheet" href="/api/v1/storefront/custom.css" />}
      <div data-tenant-css="">
        <header className={`border-b border-border bg-surface ${header.sticky ? "sticky top-0 z-40" : ""}`}>
          <div className={`mx-auto flex max-w-6xl items-center gap-6 px-4 py-3 ${header.logo_position === "center" ? "justify-center" : ""}`}>
            <Link href="/">
              {brand?.logo_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={brand.logo_url} alt={store?.name ?? "Store"} style={{ height: brand.logo_height ?? 40 }} />
              ) : <span className="font-heading text-xl font-semibold">{store?.name}</span>}
            </Link>
            <form action="/search" className="flex-1" role="search">
              <input name="q" type="search" placeholder={m("search_placeholder")} aria-label={m("search")}
                     className="w-full rounded-theme border border-border bg-bg px-3 py-2 text-sm" />
            </form>
            <form action="/api/locale" method="post"><button name="hl" value={locale === "bn" ? "en" : "bn"} className="text-sm">{m("language")}</button></form>
            <nav className="hidden gap-4 md:flex">
              {header.menu.map((m: any, i: number) => <Link key={i} href={m.href} className="text-sm">{t(m.label, locale)}</Link>)}
            </nav>
          </div>
        </header>
        {children}
        <footer className="mt-16 border-t border-border bg-surface">
          <div className="mx-auto grid max-w-6xl gap-6 px-4 py-8 md:grid-cols-4">
            {footer.columns.map((c: any, i: number) => (
              <div key={i}><p className="mb-2 font-semibold">{t(c.title, locale)}</p>
                <ul className="space-y-1 text-sm">{c.links.map((l: any, j: number) => <li key={j}><a href={l.href}>{t(l.label, locale)}</a></li>)}</ul>
              </div>
            ))}
          </div>
          <p className="pb-6 text-center text-xs text-muted">{t(footer.copyright, locale) || `© ${store?.name}`}</p>
        </footer>
      </div>
    </div>
  );
}
