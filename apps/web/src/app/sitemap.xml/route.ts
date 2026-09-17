import { headers } from "next/headers";

const API = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
const esc = (s: string) => s.replace(/[<>&'"]/g, (c) => `&#${c.charCodeAt(0)};`);

// Per-host sitemap: each tenant's primary domain lists only that tenant's visible URLs.
export async function GET() {
  const h = await headers();
  const host = h.get("x-forwarded-host") ?? h.get("host") ?? "";
  const r = await fetch(`${API}/api/v1/seo/sitemap`, { headers: { "x-forwarded-host": host }, next: { revalidate: 3600 } });
  if (!r.ok) return new Response("Not found", { status: 404 });
  const data = (await r.json()) as { primary_host: string | null; entries: { path: string; lastmod?: string | null }[] };
  const origin = `https://${data.primary_host ?? host}`;
  const body = data.entries.map((e) =>
    `<url><loc>${esc(origin + e.path)}</loc>${e.lastmod ? `<lastmod>${e.lastmod.slice(0, 10)}</lastmod>` : ""}</url>`).join("");
  return new Response(`<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${body}</urlset>`,
    { headers: { "content-type": "application/xml", "cache-control": "public, max-age=3600" } });
}
