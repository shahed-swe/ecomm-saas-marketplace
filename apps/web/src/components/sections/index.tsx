// Storefront renderer: section type -> component. Settings arrive validated by the API; all text is
// rendered as React children (escaped). No section renders raw HTML.
import Link from "next/link";
import { ProductCard, type Card } from "@/components/catalog/ProductCard";
import { ProductImage } from "@/components/catalog/ProductImage";
import { t, type Bilingual, type Locale } from "@/lib/i18n";

type S = { id: string; type: string; settings: Record<string, any>; data?: Record<string, any> | null };
type P = { s: S; locale: Locale };

const H2 = ({ text, locale }: { text: Bilingual; locale: Locale }) =>
  t(text, locale) ? <h2 className="mb-4 font-heading text-xl text-fg">{t(text, locale)}</h2> : null;

function SmartLink({ href, children, className }: { href?: string | null; children: React.ReactNode; className?: string }) {
  if (!href) return <span className={className}>{children}</span>;
  return href.startsWith("/") ? <Link href={href} className={className}>{children}</Link>
    : <a href={href} className={className} rel="noopener noreferrer nofollow" target="_blank">{children}</a>;
}

const Img = ({ src, alt, className = "" }: { src: string; alt: string; className?: string }) => (
  // eslint-disable-next-line @next/next/no-img-element
  <img src={src} alt={alt} loading="lazy" decoding="async" className={className} />
);

function Products({ items, columns = 4 }: { items: Card[]; columns?: number }) {
  const cols = { 2: "md:grid-cols-2", 3: "md:grid-cols-3", 4: "md:grid-cols-4" }[columns] ?? "md:grid-cols-4";
  return <div className={`grid grid-cols-2 gap-4 ${cols}`}>{items.map((p) => <ProductCard key={p.id} p={p} />)}</div>;
}

const BADGES: Record<string, [string, string]> = {
  cod: ["Cash on delivery", "ক্যাশ অন ডেলিভারি"], easy_return: ["Easy returns", "সহজ রিটার্ন"],
  original: ["100% original", "১০০% আসল"], fast_delivery: ["Fast delivery", "দ্রুত ডেলিভারি"],
  secure_payment: ["Secure payment", "নিরাপদ পেমেন্ট"], support: ["Customer support", "গ্রাহক সেবা"],
};

const REGISTRY: Record<string, (p: P) => React.ReactNode> = {
  announcement_bar: ({ s, locale }) => (
    <div className={`px-4 py-2 text-center text-sm ${s.settings.tone === "accent" ? "bg-accent" : s.settings.tone === "neutral" ? "bg-secondary" : "bg-primary"} text-primary-fg`}>
      <SmartLink href={s.settings.link?.href}>{t(s.settings.message, locale)}</SmartLink>
    </div>
  ),
  hero_slider: ({ s, locale }) => {
    const slide = s.settings.slides[0];
    const h = { small: "aspect-[3/1]", medium: "aspect-[5/2]", large: "aspect-[2/1]" }[s.settings.height as string];
    return (
      <div className={`relative overflow-hidden rounded-theme ${h}`}>
        <Img src={slide.image_url} alt={t(slide.heading, locale)} className="absolute inset-0 h-full w-full object-cover" />
        <div className="absolute inset-0 flex flex-col justify-end gap-2 bg-gradient-to-t from-black/50 p-6 text-white">
          <p className="font-heading text-3xl">{t(slide.heading, locale)}</p>
          <p>{t(slide.subheading, locale)}</p>
          {slide.cta && <SmartLink href={slide.cta.href} className="w-fit rounded-theme bg-primary px-4 py-2 text-primary-fg">{t(slide.cta.label, locale)}</SmartLink>}
        </div>
      </div>
    );
  },
  image_banner: ({ s, locale }) => (
    <SmartLink href={s.settings.link}><Img src={s.settings.image_url} alt={t(s.settings.alt, locale)} className="w-full rounded-theme" /></SmartLink>
  ),
  banner_grid: ({ s, locale }) => (
    <div className={`grid gap-4 ${s.settings.columns === 3 ? "md:grid-cols-3" : "md:grid-cols-2"}`}>
      {s.settings.banners.map((b: any, i: number) => (
        <SmartLink key={i} href={b.link}><Img src={b.image_url} alt={t(b.alt, locale)} className="w-full rounded-theme" /></SmartLink>
      ))}
    </div>
  ),
  category_grid: ({ s, locale }) => (
    <div>
      <H2 text={s.settings.title} locale={locale} />
      <div className="grid grid-cols-3 gap-3 md:grid-cols-6">
        {(s.data?.categories ?? []).map((c: any) => (
          <Link key={c.slug} href={`/c/${c.slug}`} className={`border border-border bg-surface p-4 text-center text-sm ${s.settings.style === "circle" ? "rounded-full" : "rounded-theme"}`}>
            {locale === "bn" ? c.name_bn || c.name_en : c.name_en}
          </Link>
        ))}
      </div>
    </div>
  ),
  product_carousel: ({ s, locale }) => (
    <div>
      <div className="flex items-baseline justify-between">
        <H2 text={s.settings.title} locale={locale} />
        {s.settings.view_all && <SmartLink href={s.settings.view_all} className="text-sm text-primary">View all</SmartLink>}
      </div>
      <div className="flex snap-x gap-4 overflow-x-auto pb-2">
        {(s.data?.products ?? []).map((p: Card) => <div key={p.id} className="w-40 shrink-0 snap-start md:w-56"><ProductCard p={p} /></div>)}
      </div>
    </div>
  ),
  product_grid: ({ s, locale }) => (
    <div><H2 text={s.settings.title} locale={locale} /><Products items={s.data?.products ?? []} columns={s.settings.columns} /></div>
  ),
  flash_sale: ({ s, locale }) => (
    <div className="rounded-theme border border-accent p-4">
      <div className="mb-3 flex items-center justify-between">
        <H2 text={s.settings.title} locale={locale} />
        <time dateTime={s.settings.ends_at} className="text-sm text-accent">Ends {new Date(s.settings.ends_at).toLocaleString()}</time>
      </div>
      <Products items={s.data?.products ?? []} />
    </div>
  ),
  campaign_strip: ({ s, locale }) => (
    <SmartLink href={s.settings.link?.href} className="flex items-center justify-between rounded-theme bg-secondary p-4 text-primary-fg">
      <span className="font-heading text-lg">{t(s.settings.heading, locale)}</span>
      {s.settings.link && <span>{t(s.settings.link.label, locale)} →</span>}
    </SmartLink>
  ),
  brand_strip: ({ s, locale }) => (
    <div><H2 text={s.settings.title} locale={locale} />
      <div className="flex flex-wrap gap-3">{(s.data?.brands ?? []).map((b: any) => (
        <span key={b.slug} className="rounded-theme border border-border px-4 py-2 text-sm">{b.name}</span>))}</div>
    </div>
  ),
  top_vendors: ({ s, locale }) => (
    <div><H2 text={s.settings.title} locale={locale} />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">{(s.data?.vendors ?? []).map((v: any) => (
        <Link key={v.slug} href={`/store/${v.slug}`} className="rounded-theme border border-border p-3">
          <p className="font-semibold">{v.display_name}</p><p className="text-xs text-muted">{v.product_count} products</p>
        </Link>))}</div>
    </div>
  ),
  featured_vendor: ({ s, locale }) => s.data?.vendor ? (
    <Link href={`/store/${s.data.vendor.slug}`} className="block rounded-theme bg-surface p-6">
      <p className="font-heading text-xl">{s.data.vendor.display_name}</p><p className="text-muted">{t(s.settings.blurb, locale)}</p>
    </Link>
  ) : null,
  rich_text: ({ s, locale }) => (
    <div className={s.settings.align === "center" ? "text-center" : ""}>
      <H2 text={s.settings.heading} locale={locale} />
      <p className="whitespace-pre-line text-fg">{t(s.settings.body, locale)}</p>
    </div>
  ),
  image_with_text: ({ s, locale }) => (
    <div className={`grid items-center gap-6 md:grid-cols-2 ${s.settings.image_side === "right" ? "md:[&>*:first-child]:order-2" : ""}`}>
      <Img src={s.settings.image_url} alt={t(s.settings.heading, locale)} className="w-full rounded-theme" />
      <div><H2 text={s.settings.heading} locale={locale} /><p className="whitespace-pre-line">{t(s.settings.body, locale)}</p>
        {s.settings.cta && <SmartLink href={s.settings.cta.href} className="mt-4 inline-block rounded-theme bg-primary px-4 py-2 text-primary-fg">{t(s.settings.cta.label, locale)}</SmartLink>}</div>
    </div>
  ),
  faq: ({ s, locale }) => (
    <div><H2 text={s.settings.title} locale={locale} />
      {s.settings.items.map((it: any, i: number) => (
        <details key={i} className="border-b border-border py-3"><summary className="cursor-pointer font-medium">{t(it.question, locale)}</summary>
          <p className="mt-2 whitespace-pre-line text-muted">{t(it.answer, locale)}</p></details>))}
    </div>
  ),
  testimonials: ({ s, locale }) => (
    <div><H2 text={s.settings.title} locale={locale} />
      <p className="mb-2 text-xs text-muted">Shared by the store</p>
      <div className="grid gap-4 md:grid-cols-3">{s.settings.items.map((it: any, i: number) => (
        <blockquote key={i} className="rounded-theme bg-surface p-4"><p>“{t(it.quote, locale)}”</p><footer className="mt-2 text-sm text-muted">— {it.name}</footer></blockquote>))}</div>
    </div>
  ),
  newsletter: ({ s, locale }) => (
    <div className="rounded-theme bg-surface p-6 text-center"><H2 text={s.settings.heading} locale={locale} /></div>
  ),
  trust_badges: ({ s, locale }) => (
    <ul className="grid grid-cols-2 gap-3 md:grid-cols-4">{s.settings.badges.map((b: string) => (
      <li key={b} className="rounded-theme border border-border p-3 text-center text-sm">{locale === "bn" ? BADGES[b][1] : BADGES[b][0]}</li>))}</ul>
  ),
  video_embed: ({ s, locale }) => {
    const u = new URL(s.settings.url);
    const id = u.hostname === "youtu.be" ? u.pathname.slice(1) : u.searchParams.get("v");
    const src = u.hostname.includes("youtu") && id ? `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}` : s.settings.url;
    return (
      <div><H2 text={s.settings.title} locale={locale} />
        <div className="aspect-video overflow-hidden rounded-theme"><iframe src={src} title={t(s.settings.title, locale) || "video"} loading="lazy"
          className="h-full w-full" sandbox="allow-scripts allow-same-origin allow-presentation" allow="encrypted-media; picture-in-picture" /></div>
      </div>
    );
  },
  spacer: ({ s }) => <div className={{ sm: "h-4", md: "h-10", lg: "h-20" }[s.settings.size as string]}>{s.settings.divider && <hr className="border-border" />}</div>,
};

export function Sections({ sections, locale }: { sections: S[]; locale: Locale }) {
  return (
    <div className="space-y-10">
      {sections.map((s) => {
        const C = REGISTRY[s.type];
        return C ? <section key={s.id} data-section={s.type} data-section-id={s.id}><C s={s} locale={locale} /></section> : null;
      })}
    </div>
  );
}

export { ProductImage };
