import { getPublishedTheme, getStore } from "@/lib/api";
import { themeToCss } from "@/lib/theme";

// Storefront shell: tenant tokens become CSS variables before first paint (no FOUC, no rebuild).
export default async function ShopLayout({ children }: { children: React.ReactNode }) {
  const [theme, store] = await Promise.all([getPublishedTheme(), getStore()]);
  const brand = theme?.document.brand;
  return (
    <div data-surface="shop" className="min-h-screen bg-bg text-fg">
      {theme && <style id="tenant-theme" dangerouslySetInnerHTML={{ __html: themeToCss(theme.document) }} />}
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3">
          {brand?.logo_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={brand.logo_url} alt={store?.name ?? "Store"} style={{ height: brand.logo_height ?? 40 }} />
          ) : (
            <span className="font-heading text-xl font-semibold">{store?.name}</span>
          )}
        </div>
      </header>
      {children}
    </div>
  );
}
