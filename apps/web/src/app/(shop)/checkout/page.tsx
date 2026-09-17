"use client";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/client-api";
import { taka } from "@/lib/money";

type District = { code: string; name_en: string; division_name: string };
type Quote = { groups: any[]; shipping_total: string; discount_total: string; vat_total: string; grand_total: string;
  payment_methods: string[]; issues: { message: string }[]; store_credit_available?: string };

export default function CheckoutPage() {
  const router = useRouter();
  const [districts, setDistricts] = useState<District[]>([]);
  const [addresses, setAddresses] = useState<any[]>([]);
  const [addressId, setAddressId] = useState<string>("");
  const [district, setDistrict] = useState("dhaka");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [method, setMethod] = useState("cod");
  const [error, setError] = useState("");
  const [useCredit, setUseCredit] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<District[]>("/geo/districts").then(setDistricts).catch(() => {});
    api<any[]>("/me/addresses").then((a) => {
      setAddresses(a);
      const def = a.find((x) => x.is_default) ?? a[0];
      if (def) { setAddressId(def.id); setDistrict(def.district_code); }
    }).catch(() => {});
  }, []);
  useEffect(() => {
    api<Quote>("/cart/quote", { method: "POST", json: { district_code: district } })
      .then((q) => { setQuote(q); if (!q.payment_methods.includes(method)) setMethod(q.payment_methods[0] ?? "cod"); })
      .catch((e) => setError(e instanceof ApiError ? e.detail : "Could not price your cart"));
  }, [district]); // eslint-disable-line react-hooks/exhaustive-deps

  async function place(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!quote) return;
    const f = new FormData(e.currentTarget);
    setBusy(true); setError("");
    const body: Record<string, unknown> = {
      payment_method: method, expected_total: quote.grand_total,
      use_store_credit: useCredit && method !== "cod",
    };
    if (addressId) body.address_id = addressId;
    else body.address = {
      recipient_name: f.get("recipient_name"), phone: f.get("phone"), district_code: district,
      upazila: f.get("upazila"), address_line: f.get("address_line"),
    };
    try {
      const r = await api<{ number: string; amount_due: string }>("/checkout/place", {
        method: "POST", json: body, headers: { "idempotency-key": crypto.getRandomValues(new Uint8Array(16)).reduce((s, b) => s + b.toString(16).padStart(2, "0"), "") },
      });
      // Prepaid orders go to the gateway, unless store credit already covered the whole total.
      const settled = method === "cod" || Number(r.amount_due ?? 0) <= 0;
      router.push(settled ? `/orders/${r.number}` : `/orders/${r.number}/payment`);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not place the order");
      if (err instanceof ApiError && err.status === 409) api<Quote>("/cart/quote", { method: "POST", json: { district_code: district } }).then(setQuote);
    } finally { setBusy(false); }
  }

  return (
    <main className="mx-auto grid max-w-4xl gap-6 p-4 md:grid-cols-2">
      <form onSubmit={place} className="space-y-3">
        <h1 className="font-heading text-2xl">Delivery</h1>
        {addresses.length > 0 && (
          <select className="w-full rounded-theme border border-border bg-bg p-2" value={addressId} aria-label="Saved address"
                  onChange={(e) => { setAddressId(e.target.value); const a = addresses.find((x) => x.id === e.target.value); if (a) setDistrict(a.district_code); }}>
            {addresses.map((a) => <option key={a.id} value={a.id}>{a.recipient_name}, {a.address_line}</option>)}
            <option value="">New address…</option>
          </select>
        )}
        {!addressId && (
          <>
            <input name="recipient_name" required placeholder="Full name" className="w-full rounded-theme border border-border bg-bg p-2" />
            <input name="phone" required placeholder="01XXXXXXXXX" className="w-full rounded-theme border border-border bg-bg p-2" />
            <select value={district} onChange={(e) => setDistrict(e.target.value)} aria-label="District" className="w-full rounded-theme border border-border bg-bg p-2">
              {districts.map((d) => <option key={d.code} value={d.code}>{d.name_en} ({d.division_name})</option>)}
            </select>
            <input name="upazila" required placeholder="Upazila / Thana" className="w-full rounded-theme border border-border bg-bg p-2" />
            <input name="address_line" required placeholder="House, road, area" className="w-full rounded-theme border border-border bg-bg p-2" />
          </>
        )}
        {quote && Number(quote.store_credit_available ?? 0) > 0 && method !== "cod" && (
          <label className="flex items-center gap-2 rounded-theme border border-border p-3 text-sm">
            <input type="checkbox" checked={useCredit} onChange={(e) => setUseCredit(e.target.checked)} />
            Use my store credit ({taka(quote.store_credit_available)})
          </label>
        )}
        <fieldset className="space-y-1">
          <legend className="text-sm font-semibold">Payment</legend>
          {(quote?.payment_methods ?? []).map((pm) => (
            <label key={pm} className="flex items-center gap-2 text-sm">
              <input type="radio" name="pm" checked={method === pm} onChange={() => setMethod(pm)} />
              {pm === "cod" ? "Cash on delivery" : pm === "bkash" ? "bKash" : "Card / SSLCommerz"}
            </label>
          ))}
        </fieldset>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <button disabled={busy || !quote} className="w-full rounded-theme bg-primary py-3 text-primary-fg disabled:opacity-50">
          {busy ? "Placing…" : `Place order ${quote ? taka(quote.grand_total) : ""}`}
        </button>
      </form>
      <aside className="space-y-2 rounded-theme border border-border p-4 text-sm">
        {quote?.groups.map((g: any) => (
          <div key={g.vendor_id} className="border-b border-border pb-2">
            {g.items.map((i: any) => <p key={i.variant_id}>{i.qty} × {i.title} <span className="float-right">{taka(i.line_total)}</span></p>)}
            <p className="text-muted">Delivery <span className="float-right">{taka(g.shipping_fee)}</span></p>
          </div>
        ))}
        {quote && <>
          <p>Discount <span className="float-right">−{taka(quote.discount_total)}</span></p>
          <p>VAT <span className="float-right">{taka(quote.vat_total)}</span></p>
          <p className="text-lg font-semibold">Total <span className="float-right">{taka(quote.grand_total)}</span></p>
        </>}
      </aside>
    </main>
  );
}
