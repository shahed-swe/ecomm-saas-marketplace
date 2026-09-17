import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getStore } from "@/lib/api";
import { ProductImage } from "@/components/catalog/ProductImage";
import { apiFetch } from "@/lib/api";
import { taka } from "@/lib/money";

type Pdp = {
  id: string; slug: string; title_en: string; title_bn?: string | null; description: string;
  variants: { id: string; sku: string; options: Record<string, string>; price: string; compare_at_price?: string | null; available: number }[];
  media: { id: string; renditions: Record<string, Record<string, string>>; blur_data?: string | null; alt_text?: string | null }[];
  vendor: { slug: string; display_name: string; district?: string | null; return_policy?: string | null; is_house: boolean };
  category: { slug: string; name_en: string };
};

async function load(slug: string): Promise<Pdp | null> {
  const r = await apiFetch(`/api/v1/catalog/products/${encodeURIComponent(slug)}`, { tags: ["products", `product:${slug}`] });
  return r.ok ? r.json() : null;
}

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }): Promise<Metadata> {
  const { slug } = await params;
  const [p, store] = await Promise.all([load(slug), getStore()]);
  if (!p) return {};
  const image = p.media[0]?.renditions?.jpeg ? Object.values(p.media[0].renditions.jpeg)[0] : undefined;
  const canonical = store?.primary_host ? `https://${store.primary_host}/p/${p.slug}` : undefined;
  return {
    title: p.title_en, description: p.description.slice(0, 160),
    alternates: { canonical },
    openGraph: { title: p.title_en, description: p.description.slice(0, 160), images: image ? [image] : [], type: "website" },
  };
}

export default async function ProductPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const p = await load(slug);
  if (!p) notFound();
  const first = p.variants[0];
  const store = await getStore();
  const origin = store?.primary_host ? `https://${store.primary_host}` : "";
  const prices = p.variants.map((v) => Number(v.price));
  const jsonLd = {
    "@context": "https://schema.org", "@type": "Product", name: p.title_en, sku: first?.sku,
    description: p.description.slice(0, 500),
    image: p.media.map((m) => Object.values(m.renditions.jpeg ?? {})[0]).filter(Boolean).map((u) => origin + u),
    offers: { "@type": "AggregateOffer", priceCurrency: "BDT", lowPrice: Math.min(...prices), highPrice: Math.max(...prices),
      offerCount: p.variants.length,
      availability: p.variants.some((v) => v.available > 0) ? "https://schema.org/InStock" : "https://schema.org/OutOfStock",
      seller: { "@type": "Organization", name: p.vendor.is_house ? store?.name : p.vendor.display_name } },
  };
  const crumbs = { "@context": "https://schema.org", "@type": "BreadcrumbList", itemListElement: [
    { "@type": "ListItem", position: 1, name: p.category.name_en, item: `${origin}/c/${p.category.slug}` },
    { "@type": "ListItem", position: 2, name: p.title_en, item: `${origin}/p/${p.slug}` }] };
  // JSON.stringify output escaped so a product title can never close the script tag
  const ld = (o: unknown) => JSON.stringify(o).replace(/</g, "\\u003c");
  return (
    <main className="mx-auto grid max-w-6xl gap-8 p-4 md:grid-cols-2">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: ld(jsonLd) }} />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: ld(crumbs) }} />
      <div className="space-y-3">
        {p.media.slice(0, 1).map((m) => (
          <ProductImage key={m.id} renditions={m.renditions} blur={m.blur_data} alt={m.alt_text ?? p.title_en}
                        sizes="(min-width: 768px) 50vw, 100vw" priority />
        ))}
      </div>
      <div className="space-y-4">
        <a href={`/c/${p.category.slug}`} className="text-sm text-muted">{p.category.name_en}</a>
        <h1 className="font-heading text-2xl text-fg">{p.title_en}</h1>
        {p.title_bn && <p className="text-muted">{p.title_bn}</p>}
        {first && (
          <p className="text-2xl font-semibold">
            {taka(first.price)}{" "}
            {first.compare_at_price && <s className="text-base text-muted">{taka(first.compare_at_price)}</s>}
          </p>
        )}
        <ul className="flex flex-wrap gap-2">
          {p.variants.map((v) => (
            <li key={v.id} className={`rounded-theme border border-border px-3 py-1 text-sm ${v.available ? "" : "opacity-40 line-through"}`}>
              {Object.values(v.options).join(" / ") || v.sku}
            </li>
          ))}
        </ul>
        {!p.vendor.is_house && (
          <section className="rounded-theme border border-border bg-surface p-4 text-sm">
            <p className="font-semibold">Sold by <a href={`/store/${p.vendor.slug}`} className="underline">{p.vendor.display_name}</a></p>
            {p.vendor.district && <p className="text-muted">Ships from {p.vendor.district}</p>}
            {p.vendor.return_policy && <p className="mt-2 text-muted">{p.vendor.return_policy}</p>}
          </section>
        )}
        <p className="whitespace-pre-line text-fg">{p.description}</p>
      </div>
    </main>
  );
}
