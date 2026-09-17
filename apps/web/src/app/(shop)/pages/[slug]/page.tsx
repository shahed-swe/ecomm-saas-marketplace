import { notFound } from "next/navigation";
import { Sections } from "@/components/sections";
import { getStore } from "@/lib/api";
import { t } from "@/lib/i18n";
import { getPage } from "@/lib/storefront";

export default async function CustomPage({ params, searchParams }: {
  params: Promise<{ slug: string }>; searchParams: Promise<{ preview?: string }>;
}) {
  const [{ slug }, { preview }] = await Promise.all([params, searchParams]);
  const [store, page] = await Promise.all([getStore(), getPage("custom", { slug, preview })]);
  if (!store || !page) notFound();
  const locale = store.default_locale as "bn" | "en";
  return (
    <main className="mx-auto max-w-4xl p-4">
      <h1 className="mb-6 font-heading text-3xl">{t(page.title, locale)}</h1>
      <Sections sections={page.sections} locale={locale} />
    </main>
  );
}
