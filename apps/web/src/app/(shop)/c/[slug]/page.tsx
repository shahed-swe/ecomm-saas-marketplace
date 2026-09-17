import { ProductCard, type Card } from "@/components/catalog/ProductCard";
import { apiFetch } from "@/lib/api";

export default async function CategoryPage({ params, searchParams }: {
  params: Promise<{ slug: string }>; searchParams: Promise<{ cursor?: string }>;
}) {
  const { slug } = await params;
  const { cursor } = await searchParams;
  const qs = new URLSearchParams({ category: slug, limit: "24", ...(cursor ? { cursor } : {}) });
  const r = await apiFetch(`/api/v1/catalog/products?${qs}`, { tags: ["products", "categories"] });
  const page = r.ok ? ((await r.json()) as { items: Card[]; next_cursor: string | null }) : { items: [], next_cursor: null };
  return (
    <main className="mx-auto max-w-6xl p-4">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {page.items.map((p, i) => <ProductCard key={p.id} p={p} priority={i < 4} />)}
      </div>
      {page.items.length === 0 && <p className="py-16 text-center text-muted">No products yet.</p>}
      {page.next_cursor && (
        <a className="mt-8 block text-center text-primary underline" href={`?cursor=${page.next_cursor}`}>Load more</a>
      )}
    </main>
  );
}
