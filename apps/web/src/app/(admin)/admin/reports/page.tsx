"use client";
// Tenant dashboard: the numbers staff check in the morning, with today included.
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/client-api";
import { taka } from "@/lib/money";

type Summary = {
  orders: number; units: number; gmv: string; refunds: string; net_sales: string;
  commission: string; average_order_value: string; delivered: number; returned: number;
  cod_share: number | null; return_rate: number | null; new_buyers: number;
};
type Report = {
  start: string; end: string; summary: Summary; previous: Summary;
  series: { day: string; gmv: string; orders: number }[];
  top_products: { title: string; units: number; revenue: string }[];
  top_vendors: { display_name: string; gmv: string; orders: number }[];
};

function delta(now: string | number, before: string | number) {
  const a = Number(now), b = Number(before);
  if (!b) return null;
  return Math.round(((a - b) / b) * 100);
}

function Card({ label, value, change }: { label: string; value: string; change?: number | null }) {
  return (
    <div className="rounded-theme border border-border p-4">
      <p className="text-sm text-muted">{label}</p>
      <p className="text-2xl font-semibold">{value}</p>
      {change != null && (
        <p className={`text-sm ${change >= 0 ? "text-success" : "text-danger"}`}>
          {change >= 0 ? "▲" : "▼"} {Math.abs(change)}% vs previous period
        </p>
      )}
    </div>
  );
}

export default function ReportsPage() {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [days, setDays] = useState(30);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    const end = new Date();
    const start = new Date(Date.now() - (days - 1) * 86400000);
    const iso = (d: Date) => d.toISOString().slice(0, 10);
    api<Report>(`/admin/reports/overview?start=${iso(start)}&end=${iso(end)}`)
      .then(setReport)
      .catch((e) => setError(e instanceof ApiError ? e.detail : "Could not load the report"));
  }, [days]);

  async function exportCsv() {
    setExporting(true);
    try {
      const out = await api<{ url: string }>("/admin/reports/exports?kind=orders", { method: "POST" });
      window.open(out.url, "_blank");
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Export failed");
    } finally { setExporting(false); }
  }

  if (error) return <main className="p-6 text-danger">{error}</main>;
  if (!report) return <main className="p-6 text-muted">Loading…</main>;
  const max = Math.max(...report.series.map((d) => Number(d.gmv)), 1);
  return (
    <main className="mx-auto max-w-5xl space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="font-heading text-2xl">Reports</h1>
        <div className="flex items-center gap-2">
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period"
                  className="rounded-theme border border-border bg-bg p-2 text-sm">
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
          <button onClick={exportCsv} disabled={exporting}
                  className="rounded-theme border border-border px-3 py-2 text-sm disabled:opacity-50">
            {exporting ? "Preparing…" : "Export orders"}
          </button>
        </div>
      </div>

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card label="Sales" value={taka(report.summary.gmv)} change={delta(report.summary.gmv, report.previous.gmv)} />
        <Card label="Net of refunds" value={taka(report.summary.net_sales)} />
        <Card label="Orders" value={String(report.summary.orders)} change={delta(report.summary.orders, report.previous.orders)} />
        <Card label="Average order" value={taka(report.summary.average_order_value)} />
        <Card label="Commission" value={taka(report.summary.commission)} />
        <Card label="Delivered" value={String(report.summary.delivered)} />
        <Card label="Returns" value={report.summary.return_rate != null ? `${(report.summary.return_rate * 100).toFixed(1)}%` : "—"} />
        <Card label="Cash on delivery" value={report.summary.cod_share != null ? `${(report.summary.cod_share * 100).toFixed(0)}%` : "—"} />
      </section>

      <section className="rounded-theme border border-border p-4">
        <h2 className="font-heading text-lg">Daily sales</h2>
        <ol className="mt-3 flex h-40 items-end gap-1" aria-label="Daily sales">
          {report.series.map((d) => (
            <li key={d.day} className="flex-1" title={`${d.day}: ${taka(d.gmv)} · ${d.orders} orders`}>
              <span className="block w-full rounded-t bg-primary"
                    style={{ height: `${Math.max((Number(d.gmv) / max) * 100, 2)}%` }} />
            </li>
          ))}
        </ol>
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        <section className="rounded-theme border border-border p-4">
          <h2 className="font-heading text-lg">Top products</h2>
          <ul className="mt-2 space-y-1 text-sm">
            {report.top_products.map((p) => (
              <li key={p.title} className="flex justify-between gap-4">
                <span className="truncate">{p.title}</span>
                <span className="text-muted">{p.units} · {taka(p.revenue)}</span>
              </li>
            ))}
          </ul>
        </section>
        <section className="rounded-theme border border-border p-4">
          <h2 className="font-heading text-lg">Top sellers</h2>
          <ul className="mt-2 space-y-1 text-sm">
            {report.top_vendors.map((v) => (
              <li key={v.display_name} className="flex justify-between gap-4">
                <span className="truncate">{v.display_name}</span>
                <span className="text-muted">{v.orders} · {taka(v.gmv)}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </main>
  );
}
