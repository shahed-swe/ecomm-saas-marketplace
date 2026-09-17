export async function POST(req: Request) {
  const form = await req.formData();
  const hl = form.get("hl") === "en" ? "en" : "bn";
  const back = new URL(req.headers.get("referer") ?? "/", req.url);
  return new Response(null, {
    status: 303,
    headers: { location: back.pathname + back.search, "set-cookie": `hl=${hl}; Path=/; Max-Age=31536000; SameSite=Lax` },
  });
}
