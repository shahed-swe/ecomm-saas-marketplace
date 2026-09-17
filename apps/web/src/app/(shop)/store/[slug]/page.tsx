import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { ProductCard, type Card } from "@/components/catalog/ProductCard";
import { Sections } from "@/components/sections";
import { apiFetch } from "@/lib/api";
import { getLocale } from "@/lib/locale";

type Store = { vendor: { display_name: string; tagline?: string; bio?: string; district?: string }; accent?: string | null;
  banner_url?: string | null; sections: any[]; products: Card[] };

async function load(slug: string): Promise<Store | null> {
  const r = await apiFetch(`/api/v1/storefront/stores/${encodeURIComponent(slug)}`, { tags: ["products", "stores"] });
  return r.ok ? r.json() : null;
}

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }): Promise<Metadata> {
  const s = await load((await params).slug);
  return s ? { title: s.vendor.display_name, description: s.vendor.tagline ?? s.vendor.bio?.slice(0, 150) } : {};
}

export default async function StorePage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const [store, locale] = await Promise.all([load(slug), getLocale()]);
  if (!store) notFound();
  const accent = store.accent && /^\d{1,3} \d{1,3} \d{1,3}$/.test(store.accent) ? store.accent : null;
  return (
    <main className="mx-auto max-w-6xl p-4" style={accent ? ({ "--color-primary": accent } as React.CSSProperties) : undefined}>
      {store.banner_url && (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={store.banner_url} alt="" className="mb-4 aspect-[5/1] w-full rounded-theme object-cover" />
      )}
      <h1 className="font-heading text-3xl">{store.vendor.display_name}</h1>
      {store.vendor.tagline && <p className="text-muted">{store.vendor.tagline}</p>}
      <div className="my-8"><Sections sections={store.sections} locale={locale} /></div>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">{store.products.map((p) => <ProductCard key={p.id} p={p} />)}</div>
    </main>
  );
}
