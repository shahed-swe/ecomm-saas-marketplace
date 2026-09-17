"use client";
import { use, useEffect, useState } from "react";
import { api } from "@/lib/client-api";
import { taka } from "@/lib/money";

export default function OrderPage({ params }: { params: Promise<{ number: string }> }) {
  const { number } = use(params);
  const [order, setOrder] = useState<any>(null);
  const [error, setError] = useState("");
  useEffect(() => { api<any>(`/me/orders/${number}`).then(setOrder).catch(() => setError("Order not found")); }, [number]);
  if (error) return <main className="p-8 text-center text-muted">{error}</main>;
  if (!order) return <main className="p-8 text-center text-muted">Loading…</main>;
  return (
    <main className="mx-auto max-w-2xl space-y-4 p-4">
      <h1 className="font-heading text-2xl">Order {order.number}</h1>
      <p className="text-muted">Status: {String(order.status).replace(/_/g, " ")}</p>
      {order.shipments.map((s: any) => (
        <div key={s.id} className="rounded-theme border border-border p-3">
          <p className="font-medium">{s.vendor_name}</p>
          <p className="text-sm text-muted">{s.number} · {String(s.status).replace(/_/g, " ")} · {taka(s.total)}</p>
        </div>
      ))}
      <p className="text-lg font-semibold">Total {taka(order.grand_total)}</p>
      {order.status === "pending_payment" && order.payment_method !== "cod" && (
        <a href={`/orders/${order.number}/payment`} className="block rounded-theme bg-primary py-3 text-center text-primary-fg">
          Pay now
        </a>
      )}
      {order.payment_method === "cod" && (
        <p className="text-sm text-muted">Cash on delivery — pay the courier {taka(order.grand_total)} when your parcel arrives.</p>
      )}
    </main>
  );
}
