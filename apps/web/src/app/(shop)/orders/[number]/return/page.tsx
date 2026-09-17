"use client";
// Asking for a return: pick the shipment, the items, a reason, and where the money should go.
import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/client-api";
import { taka } from "@/lib/money";

const REASONS = [
  ["damaged", "Arrived damaged"],
  ["wrong_item", "Wrong item sent"],
  ["not_as_described", "Not as described"],
  ["missing_parts", "Parts missing"],
  ["size_issue", "Size does not fit"],
  ["changed_mind", "Changed my mind"],
  ["other", "Something else"],
] as const;

export default function ReturnPage({ params }: { params: Promise<{ number: string }> }) {
  const { number } = use(params);
  const router = useRouter();
  const [shipments, setShipments] = useState<any[]>([]);
  const [chosen, setChosen] = useState<string>("");
  const [items, setItems] = useState<any[]>([]);
  const [qty, setQty] = useState<Record<string, number>>({});
  const [reason, setReason] = useState<string>("damaged");
  const [method, setMethod] = useState("original");
  const [wallet, setWallet] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<any[]>(`/me/orders/${number}/return-window`)
      .then((rows) => { setShipments(rows); setChosen(rows.find((r) => r.returnable)?.sub_order_id ?? ""); })
      .catch(() => setError("Order not found"));
  }, [number]);

  useEffect(() => {
    if (!chosen) return;
    // Order items carry their shipment, so the list narrows to the parcel being returned.
    api<any>(`/me/orders/${number}`)
      .then((order) => setItems((order.items ?? []).filter((i: any) => i.sub_order_id === chosen)))
      .catch(() => setItems([]));
  }, [chosen, number]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      const chosenItems = Object.entries(qty).filter(([, q]) => q > 0)
        .map(([order_item_id, q]) => ({ order_item_id, qty: q }));
      if (!chosenItems.length) { setError("Choose at least one item"); setBusy(false); return; }
      await api("/me/returns", {
        method: "POST",
        json: {
          sub_order_id: chosen, reason, note: note || null, items: chosenItems,
          refund_method: method,
          ...(method === "bkash" ? { bkash_number: wallet } : {}),
        },
      });
      router.push(`/orders/${number}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Could not send the request");
    } finally { setBusy(false); }
  }

  const shipment = shipments.find((s) => s.sub_order_id === chosen);
  return (
    <main className="mx-auto max-w-xl space-y-4 p-4">
      <h1 className="font-heading text-2xl">Return items from {number}</h1>
      {shipments.length > 0 && (
        <select value={chosen} onChange={(e) => setChosen(e.target.value)} aria-label="Shipment"
                className="w-full rounded-theme border border-border bg-bg p-2">
          {shipments.map((s) => (
            <option key={s.sub_order_id} value={s.sub_order_id} disabled={!s.returnable}>
              {s.number} {s.returnable ? "" : "— return window closed"}
            </option>
          ))}
        </select>
      )}
      {shipment && (
        <p className="text-sm text-muted">
          You can return items from this shipment for {shipment.window_days} days after delivery.
        </p>
      )}
      <form onSubmit={submit} className="space-y-3">
        {items.map((i: any) => (
          <label key={i.id} className="flex items-center justify-between rounded-theme border border-border p-3">
            <span>{i.title_snapshot ?? i.title} <span className="text-muted">· {taka(i.line_total)}</span></span>
            <input type="number" min={0} max={i.qty} value={qty[i.id] ?? 0} aria-label={`Quantity to return of ${i.title_snapshot ?? i.title}`}
                   onChange={(e) => setQty({ ...qty, [i.id]: Number(e.target.value) })}
                   className="w-16 rounded-theme border border-border bg-bg p-1 text-right" />
          </label>
        ))}
        <select value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Reason"
                className="w-full rounded-theme border border-border bg-bg p-2">
          {REASONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Anything the shop should know"
                  className="w-full rounded-theme border border-border bg-bg p-2" rows={3} />
        <fieldset className="space-y-1">
          <legend className="text-sm font-semibold">Refund to</legend>
          {[["original", "However I paid"], ["store_credit", "Store credit"], ["bkash", "A bKash number"], ["bank", "My bank account"]].map(([value, label]) => (
            <label key={value} className="flex items-center gap-2 text-sm">
              <input type="radio" name="refund" checked={method === value} onChange={() => setMethod(value)} />
              {label}
            </label>
          ))}
        </fieldset>
        {method === "bkash" && (
          <input value={wallet} onChange={(e) => setWallet(e.target.value)} placeholder="01XXXXXXXXX"
                 className="w-full rounded-theme border border-border bg-bg p-2" />
        )}
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <button disabled={busy || !chosen} className="w-full rounded-theme bg-primary py-3 text-primary-fg disabled:opacity-50">
          {busy ? "Sending…" : "Request return"}
        </button>
      </form>
    </main>
  );
}
