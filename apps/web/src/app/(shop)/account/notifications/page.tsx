"use client";
// The buyer's inbox and what they are willing to be contacted about.
import { useEffect, useState } from "react";
import { api } from "@/lib/client-api";

type Item = { id: string; key: string; title?: string | null; body: string; deep_link?: string | null; created_at: string; read_at?: string | null };
type Prefs = { marketing_push: boolean; marketing_sms: boolean; marketing_email: boolean };

export default function NotificationsPage() {
  const [items, setItems] = useState<Item[]>([]);
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api<{ items: Item[] }>("/me/notifications").then((r) => setItems(r.items)).catch(() => setItems([]));
    api<Prefs>("/me/notification-preferences").then(setPrefs).catch(() => setPrefs(null));
    api("/me/notifications/read", { method: "POST" }).catch(() => {});
  }, []);

  async function update(patch: Partial<Prefs>) {
    if (!prefs) return;
    const next = { ...prefs, ...patch };
    setPrefs(next);
    await api("/me/notification-preferences", { method: "PUT", json: next }).catch(() => {});
    setSaved(true);
  }

  return (
    <main className="mx-auto max-w-2xl space-y-6 p-4">
      <h1 className="font-heading text-2xl">Notifications</h1>
      <ul className="space-y-2">
        {items.length === 0 && <li className="text-muted">Nothing yet.</li>}
        {items.map((n) => (
          <li key={n.id} className={`rounded-theme border border-border p-3 ${n.read_at ? "" : "bg-surface"}`}>
            {n.title && <p className="font-medium">{n.title}</p>}
            <p>{n.body}</p>
            <p className="text-sm text-muted">{new Date(n.created_at).toLocaleString()}</p>
            {n.deep_link && <a href={n.deep_link} className="text-sm underline">Open</a>}
          </li>
        ))}
      </ul>
      {prefs && (
        <section className="space-y-2 rounded-theme border border-border p-4">
          <h2 className="font-heading text-lg">Offers and reminders</h2>
          <p className="text-sm text-muted">Order updates always reach you; these are the optional ones.</p>
          {([["marketing_push", "Push"], ["marketing_sms", "SMS"], ["marketing_email", "Email"]] as const).map(([key, label]) => (
            <label key={key} className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={prefs[key]} onChange={(e) => update({ [key]: e.target.checked } as Partial<Prefs>)} />
              {label}
            </label>
          ))}
          {saved && <p aria-live="polite" className="text-sm text-muted">Saved.</p>}
        </section>
      )}
    </main>
  );
}
