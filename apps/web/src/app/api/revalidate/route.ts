import { createHmac, timingSafeEqual } from "node:crypto";
import { revalidateTag } from "next/cache";

// Called by the API after catalog/theme writes (HMAC over the raw body).
export async function POST(req: Request) {
  const secret = process.env.REVALIDATE_SECRET;
  const body = await req.text();
  const sig = req.headers.get("x-signature") ?? "";
  if (!secret) return new Response("disabled", { status: 404 });
  const expected = createHmac("sha256", secret).update(body).digest("hex");
  if (sig.length !== expected.length || !timingSafeEqual(Buffer.from(sig), Buffer.from(expected))) {
    return new Response("forbidden", { status: 403 });
  }
  const { tags } = JSON.parse(body) as { tags: string[] };
  for (const t of tags.slice(0, 50)) revalidateTag(t);
  return Response.json({ revalidated: tags.length });
}
