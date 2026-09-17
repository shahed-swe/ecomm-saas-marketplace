"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, api } from "@/lib/client-api";

export function AddToCart({ variants, label }: { variants: { id: string; sku: string; options: Record<string, string>; available: number }[]; label: string }) {
  const router = useRouter();
  const [selected, setSelected] = useState(variants[0]?.id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const v = variants.find((x) => x.id === selected);
  async function add() {
    if (!v) return;
    setBusy(true); setError("");
    try {
      await api("/cart/items", { method: "POST", json: { variant_id: v.id, qty: 1 } });
      router.push("/cart");
    } catch (e) {
      setError(e instanceof ApiError && e.status === 403 ? "Please sign in to shop" : "Could not add to cart");
    } finally { setBusy(false); }
  }
  return (
    <div className="space-y-3">
      {variants.length > 1 && (
        <select className="w-full rounded-theme border border-border bg-bg p-2" value={selected} onChange={(e) => setSelected(e.target.value)} aria-label="Variant">
          {variants.map((x) => <option key={x.id} value={x.id} disabled={x.available < 1}>{Object.values(x.options).join(" / ") || x.sku}</option>)}
        </select>
      )}
      <button onClick={add} disabled={busy || !v || v.available < 1}
              className="w-full rounded-theme bg-primary px-4 py-3 text-primary-fg disabled:opacity-50">
        {v && v.available < 1 ? "Out of stock" : busy ? "Adding…" : label}
      </button>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    </div>
  );
}
