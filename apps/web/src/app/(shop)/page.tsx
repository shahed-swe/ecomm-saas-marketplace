import { notFound } from "next/navigation";
import { getStore } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ShopHome() {
  const store = await getStore();
  if (!store) notFound();
  return (
    <main className="mx-auto max-w-6xl p-6">
      <section className="rounded-theme bg-surface border border-border p-8">
        <h1 className="font-heading text-3xl text-fg">{store.name}</h1>
        <p className="mt-2 text-muted">
          {store.store_mode === "multi" ? "Marketplace" : "Store"} · sections from the page builder render here (Phase 8).
        </p>
      </section>
    </main>
  );
}
