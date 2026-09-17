"use client";
// Payment handoff and return. The gateway's answer is never read from the query string:
// the page asks our API to verify with the provider, and only then shows "paid".
import { use, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/client-api";
import { taka } from "@/lib/money";

type Payment = { id: string | null; status: string; amount?: string; provider?: string };
const DONE = ["paid", "failed", "cancelled", "refunded"];

export default function PaymentPage({ params }: { params: Promise<{ number: string }> }) {
  const { number } = use(params);
  const router = useRouter();
  const query = useSearchParams();
  const returning = query.has("status") || query.has("paymentID") || query.has("val_id");
  const [state, setState] = useState<"working" | "paid" | "failed">("working");
  const [note, setNote] = useState(returning ? "Checking with the payment provider…" : "Opening the payment page…");
  const [payment, setPayment] = useState<Payment | null>(null);
  const started = useRef(false);

  const confirm = useCallback(async (id: string) => {
    const r = await api<{ status: string }>(`/payments/${id}/confirm`, { method: "POST" });
    if (r.status === "paid") { setState("paid"); setNote("Payment received."); return true; }
    if (DONE.includes(r.status)) { setState("failed"); setNote("The payment did not go through. Nothing was charged twice — you can try again."); return true; }
    setNote("The provider has not settled this yet. We will keep checking…");
    return false;
  }, []);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    (async () => {
      try {
        const current = await api<Payment>(`/me/orders/${number}/payment`);
        setPayment(current);
        if (current.status === "paid") { setState("paid"); setNote("This order is already paid."); return; }
        if (returning && current.id) { await confirm(current.id); return; }
        const started_ = await api<{ payment_id: string; redirect_url: string }>("/payments/start", {
          method: "POST", json: { order_number: number },
        });
        window.location.href = started_.redirect_url;
      } catch (err) {
        setState("failed");
        setNote(err instanceof ApiError ? err.detail : "We could not start the payment.");
      }
    })();
  }, [number, returning, confirm]);

  useEffect(() => {
    if (state !== "working" || !returning) return;
    const t = setInterval(async () => {
      const current = await api<Payment>(`/me/orders/${number}/payment`).catch(() => null);
      if (current?.status === "paid") { setState("paid"); setNote("Payment received."); }
      else if (current?.id && DONE.includes(current.status)) { setState("failed"); setNote("The payment did not go through."); }
    }, 4000);
    return () => clearInterval(t);
  }, [state, returning, number]);

  return (
    <main className="mx-auto max-w-md space-y-4 p-8 text-center">
      <h1 className="font-heading text-2xl">Order {number}</h1>
      {payment?.amount && <p className="text-lg font-semibold">{taka(payment.amount)}</p>}
      <p aria-live="polite" className={state === "failed" ? "text-danger" : "text-muted"}>{note}</p>
      {state === "working" && <div aria-hidden className="mx-auto h-6 w-6 animate-spin rounded-full border-2 border-border border-t-primary" />}
      {state === "paid" && (
        <button onClick={() => router.push(`/orders/${number}`)} className="w-full rounded-theme bg-primary py-3 text-primary-fg">
          View order
        </button>
      )}
      {state === "failed" && (
        <div className="space-y-2">
          <button onClick={() => window.location.replace(`/orders/${number}/payment`)} className="w-full rounded-theme bg-primary py-3 text-primary-fg">
            Try again
          </button>
          <button onClick={() => router.push(`/orders/${number}`)} className="w-full rounded-theme border border-border py-3">
            Back to order
          </button>
        </div>
      )}
    </main>
  );
}
