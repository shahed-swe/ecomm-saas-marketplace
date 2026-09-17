import { apiFetch } from "./api";

export type PageData = {
  template: string; version: number; preview: boolean; title?: { en?: string; bn?: string } | null;
  sections: { id: string; type: string; settings: Record<string, any>; data?: Record<string, any> | null }[];
  layouts: { header: any; footer: any; product_layout: any; collection_layout: any };
};

export async function getPage(template: string, opts: { slug?: string; preview?: string } = {}): Promise<PageData | null> {
  const qs = new URLSearchParams({ template, ...(opts.slug ? { slug: opts.slug } : {}), ...(opts.preview ? { preview: opts.preview } : {}) });
  const r = await apiFetch(`/api/v1/storefront/page?${qs}`, opts.preview ? {} : { tags: ["theme", "products"] });
  return r.ok ? ((await r.json()) as PageData) : null;
}
