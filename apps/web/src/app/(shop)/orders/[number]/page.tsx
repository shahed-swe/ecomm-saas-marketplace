"use client";
import { use, useEffect, useState } from "react";
import { api } from "@/lib/client-api";
import { taka } from "@/lib/money";

export default function OrderPage({ params }: { params: Promise<{ number: string }> }) {
  const { number } = use(params);
  const [order, setOrder] = useState<any>(null);
  const [tracking, setTracking] = useState<any[]>([]);
  const [returns, setReturns] = useState<any[]>([]);
  const [returnable, setReturnable] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { api<any>(`/me/orders/${number}`).then(setOrder).catch(() => setError("Order not found")); }, [number]);
  // Parcel status comes from the courier, so it is fetched separately from the order itself.
  useEffect(() => { api<any[]>(`/me/orders/${number}/tracking`).then(setTracking).catch(() => setTracking([])); }, [number]);
  useEffect(() => {
    api<any[]>(`/me/orders/${number}/return-window`).then((rows) => setReturnable(rows.some((r) => r.returnable))).catch(() => setReturnable(false));
    api<any[]>("/me/returns").then((rows) => setReturns(rows.filter((r) => r.order_number === number))).catch(() => setReturns([]));
  }, [number]);
  if (error) return <main className="p-8 text-center text-muted">{error}</main>;
  if (!order) return <main className="p-8 text-center text-muted">Loading…</main>;
  return (
    <main className="mx-auto max-w-2xl space-y-4 p-4">
      <h1 className="font-heading text-2xl">Order {order.number}</h1>
      <p className="text-muted">Status: {String(order.status).replace(/_/g, " ")}</p>
      {order.shipments.map((s: any) => {
        const parcel = tracking.find((p) => p.shipment_number === s.number);
        return (
          <div key={s.id} className="rounded-theme border border-border p-3">
            <p className="font-medium">{s.vendor_name}</p>
            <p className="text-sm text-muted">{s.number} · {String(s.status).replace(/_/g, " ")} · {taka(s.total)}</p>
            {parcel && (
              <p className="mt-1 text-sm">
                {parcel.courier} · {String(parcel.status).replace(/_/g, " ")}
                {parcel.tracking_url && (
                  <> · <a className="underline" href={parcel.tracking_url} target="_blank" rel="noreferrer">Track {parcel.tracking_code}</a></>
                )}
              </p>
            )}
          </div>
        );
      })}
      <p className="text-lg font-semibold">Total {taka(order.grand_total)}</p>
      {order.status === "pending_payment" && order.payment_method !== "cod" && (
        <a href={`/orders/${order.number}/payment`} className="block rounded-theme bg-primary py-3 text-center text-primary-fg">
          Pay now
        </a>
      )}
      {returns.map((r: any) => (
        <p key={r.number} className="rounded-theme border border-border p-3 text-sm">
          Return {r.number} · {String(r.status).replace(/_/g, " ")} · {taka(r.refund_total)}
          {r.refund_method === "store_credit" ? " to store credit" : ""}
        </p>
      ))}
      {returnable && (
        <a href={`/orders/${order.number}/return`} className="block rounded-theme border border-border py-3 text-center">
          Return an item
        </a>
      )}
      {order.payment_method === "cod" && (
        <p className="text-sm text-muted">Cash on delivery — pay the courier {taka(order.grand_total)} when your parcel arrives.</p>
      )}
    </main>
  );
}
