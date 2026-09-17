"use client";
// Reviews on the PDP: the summary and the list, with a "helpful" vote for signed-in buyers.
import { useEffect, useState } from "react";
import { api } from "@/lib/client-api";

type Review = {
  id: string; rating: number; title?: string | null; body?: string | null; author: string;
  helpful_count: number; vendor_reply?: string | null; created_at: string;
};
type Summary = { rating_avg: string; rating_count: number; histogram: Record<string, number> | null; reviews: Review[] };

function Stars({ value }: { value: number }) {
  return (
    <span aria-label={`${value} out of 5`} className="text-warning">
      {"★".repeat(Math.round(value))}<span className="text-muted">{"★".repeat(5 - Math.round(value))}</span>
    </span>
  );
}

export function Reviews({ productId }: { productId: string }) {
  const [data, setData] = useState<Summary | null>(null);
  const [voted, setVoted] = useState<Record<string, boolean>>({});
  useEffect(() => { api<Summary>(`/products/${productId}/reviews`).then(setData).catch(() => setData(null)); }, [productId]);
  if (!data || !data.rating_count) return <p className="text-sm text-muted">No reviews yet.</p>;
  const histogram = data.histogram ?? {};
  return (
    <section className="space-y-4">
      <h2 className="font-heading text-xl">Customer reviews</h2>
      <p className="flex items-center gap-2">
        <Stars value={Number(data.rating_avg)} />
        <span className="font-semibold">{Number(data.rating_avg).toFixed(1)}</span>
        <span className="text-muted">({data.rating_count})</span>
      </p>
      <ul className="space-y-1 text-sm">
        {[5, 4, 3, 2, 1].map((star) => (
          <li key={star} className="flex items-center gap-2">
            <span className="w-8 text-muted">{star}★</span>
            <span className="h-2 flex-1 rounded-theme bg-surface">
              <span className="block h-2 rounded-theme bg-primary"
                    style={{ width: `${((histogram[String(star)] ?? 0) / data.rating_count) * 100}%` }} />
            </span>
            <span className="w-8 text-right text-muted">{histogram[String(star)] ?? 0}</span>
          </li>
        ))}
      </ul>
      <ul className="space-y-3">
        {data.reviews.map((r) => (
          <li key={r.id} className="rounded-theme border border-border p-3">
            <p className="flex items-center gap-2 text-sm">
              <Stars value={r.rating} />
              <span className="font-medium">{r.author}</span>
              <span className="text-muted">{new Date(r.created_at).toLocaleDateString()}</span>
            </p>
            {r.title && <p className="font-medium">{r.title}</p>}
            {r.body && <p className="text-fg">{r.body}</p>}
            {r.vendor_reply && (
              <p className="mt-2 rounded-theme bg-surface p-2 text-sm"><b>Seller:</b> {r.vendor_reply}</p>
            )}
            <button
              onClick={async () => { await api(`/reviews/${r.id}/helpful`, { method: "POST" }).catch(() => {}); setVoted({ ...voted, [r.id]: true }); }}
              disabled={voted[r.id]}
              className="mt-2 text-sm underline disabled:opacity-50">
              Helpful ({r.helpful_count + (voted[r.id] ? 1 : 0)})
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
