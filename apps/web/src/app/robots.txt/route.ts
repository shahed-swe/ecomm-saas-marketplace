import { headers } from "next/headers";

export async function GET() {
  const host = (await headers()).get("x-forwarded-host") ?? (await headers()).get("host") ?? "";
  const txt = [
    "User-agent: *",
    "Disallow: /admin", "Disallow: /vendor", "Disallow: /platform", "Disallow: /api/", "Disallow: /checkout",
    "Disallow: /account", "Disallow: /search", "Disallow: /*?preview=",
    `Sitemap: https://${host.split(":")[0]}/sitemap.xml`,
  ].join("\n");
  return new Response(txt, { headers: { "content-type": "text/plain", "cache-control": "public, max-age=3600" } });
}
