import type { Metadata } from "next";
import { ProductCard, type Card } from "@/components/catalog/ProductCard";
import { apiFetch } from "@/lib/api";
import { getMessages } from "@/lib/locale";

type Facet = { value: string; label: string; count: number };
type Result = {
  total: number; items: Card[]; did_you_mean?: string | null;
  facets: { categories: Facet[]; brands: Facet[]; stores: Facet[] };
};

export const metadata: Metadata = { robots: { index: false, follow: true } };

export default async function SearchPage({ searchParams }: { searchParams: Promise<Record<string, string | string[]>> }) {
  const sp = await searchParams;
  const { m } = await getMessages();
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(sp)) for (const x of Array.isArray(v) ? v : [v]) qs.append(k, x);
  const r = await apiFetch(`/api/v1/catalog/search?${qs}`);
  const res: Result = r.ok ? await r.json() : { total: 0, items: [], facets: { categories: [], brands: [], stores: [] } };
  const q = typeof sp.q === "string" ? sp.q : "";
  const chosen = (key: string) => ([] as string[]).concat(sp[key] ?? []);
  const group = (key: "categories" | "brands" | "stores", param: string) => res.facets[key].length > 0 && (
    <fieldset className="space-y-1">
      <legend className="mb-1 text-sm font-semibold">{m(key)}</legend>
      {res.facets[key].map((f) => (
        <label key={f.value} className="flex items-center gap-2 text-sm">
          <input type={param === "category" ? "radio" : "checkbox"} name={param} value={f.value}
                 defaultChecked={chosen(param).includes(f.value)} />
          {f.label} <span className="text-muted">({f.count})</span>
        </label>
      ))}
    </fieldset>
  );
  return (
    <main className="mx-auto grid max-w-6xl gap-6 p-4 md:grid-cols-[220px_1fr]">
      <form className="space-y-4" method="get">
        <input type="hidden" name="q" value={q} />
        {group("categories", "category")}
        {group("brands", "brand")}
        {group("stores", "store")}
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" name="in_stock" value="true" defaultChecked={sp.in_stock === "true"} />{m("in_stock")}</label>
        <select name="sort" defaultValue={(sp.sort as string) ?? "relevance"} className="w-full rounded-theme border border-border bg-bg p-2 text-sm" aria-label={m("sort")}>
          {(["relevance", "newest", "price_asc", "price_desc"] as const).map((s) => <option key={s} value={s}>{m(s)}</option>)}
        </select>
        <button className="w-full rounded-theme bg-primary py-2 text-sm text-primary-fg">{m("apply")}</button>
      </form>
      <section>
        {res.did_you_mean && <p className="mb-2 text-sm">{m("did_you_mean")} <strong>{res.did_you_mean}</strong></p>}
        <p className="mb-4 text-sm text-muted">{m("results", { n: res.total })}</p>
        {res.items.length === 0 ? <p className="py-16 text-center text-muted">{m("no_results")}</p> : (
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-4">
            {res.items.map((p, i) => <ProductCard key={p.id} p={p} priority={i < 4} />)}
          </div>
        )}
      </section>
    </main>
  );
}
