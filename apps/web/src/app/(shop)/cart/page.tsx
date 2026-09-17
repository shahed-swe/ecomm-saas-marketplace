"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/client-api";
import { taka } from "@/lib/money";

type Quote = { groups: { vendor_id: string; items: any[]; subtotal: string; shipping_fee: string; total: string }[];
  items_subtotal: string; shipping_total: string; grand_total: string; issues: { message: string }[] };

export default function CartPage() {
  const [cart, setCart] = useState<{ items: { variant_id: string; qty: number }[]; quote: Quote | null } | null>(null);
  const [error, setError] = useState("");
  const load = () => api<any>("/cart").then(setCart).catch((e) => setError(e instanceof ApiError && e.status === 403 ? "Sign in to see your cart" : "Could not load your cart"));
  useEffect(() => { load(); }, []);
  async function setQty(variantId: string, qty: number) {
    await api(`/cart/items/${variantId}`, { method: "PATCH", json: { qty } });
    load();
  }
  if (error) return <main className="p-8 text-center text-muted">{error}</main>;
  if (!cart) return <main className="p-8 text-center text-muted">Loading…</main>;
  if (!cart.quote) return <main className="p-16 text-center text-muted">Your cart is empty. <Link href="/" className="text-primary underline">Start shopping</Link></main>;
  return (
    <main className="mx-auto max-w-4xl space-y-6 p-4">
      <h1 className="font-heading text-2xl">Cart</h1>
      {cart.quote.issues.map((i, n) => <p key={n} className="rounded-theme bg-surface p-3 text-sm text-danger">{i.message}</p>)}
      {cart.quote.groups.map((g) => (
        <section key={g.vendor_id} className="rounded-theme border border-border p-4">
          {g.items.map((it) => (
            <div key={it.variant_id} className="flex items-center justify-between gap-4 border-b border-border py-2 last:border-0">
              <div><p>{it.title}</p><p className="text-xs text-muted">{Object.values(it.options ?? {}).join(" / ")}</p></div>
              <div className="flex items-center gap-2">
                <input type="number" min={0} max={20} defaultValue={it.qty} aria-label="Quantity"
                       onBlur={(e) => setQty(it.variant_id, Number(e.target.value))}
                       className="w-16 rounded-theme border border-border bg-bg p-1 text-center" />
                <span className="w-24 text-right font-medium">{taka(it.line_total)}</span>
              </div>
            </div>
          ))}
          <p className="pt-2 text-right text-sm text-muted">Delivery {taka(g.shipping_fee)}</p>
        </section>
      ))}
      <div className="flex items-center justify-between text-lg font-semibold">
        <span>Total</span><span>{taka(cart.quote.grand_total)}</span>
      </div>
      <Link href="/checkout" className="block rounded-theme bg-primary py-3 text-center text-primary-fg">Checkout</Link>
    </main>
  );
}
