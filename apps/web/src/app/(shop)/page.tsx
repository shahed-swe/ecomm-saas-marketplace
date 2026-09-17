import { notFound } from "next/navigation";
import { Sections } from "@/components/sections";
import { getStore } from "@/lib/api";
import { getPage } from "@/lib/storefront";

export const dynamic = "force-dynamic";

export default async function ShopHome({ searchParams }: { searchParams: Promise<{ preview?: string }> }) {
  const { preview } = await searchParams;
  const [store, page] = await Promise.all([getStore(), getPage("home", { preview })]);
  if (!store || !page) notFound();
  return (
    <main className="mx-auto max-w-6xl p-4">
      <Sections sections={page.sections} locale={store.default_locale as "bn" | "en"} />
    </main>
  );
}
