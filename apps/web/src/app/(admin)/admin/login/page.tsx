"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, api, setToken } from "@/lib/client-api";

export default function StaffLogin() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true); setError("");
    try {
      const r = await api<{ access_token: string }>("/auth/login", {
        method: "POST", json: { email: f.get("email"), password: f.get("password"), surface: "staff" },
      });
      setToken(r.access_token);
      router.push("/admin/builder");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Sign-in failed");
    } finally { setBusy(false); }
  }
  return (
    <main className="mx-auto mt-24 max-w-sm rounded-theme border border-border bg-surface p-6">
      <h1 className="mb-4 font-heading text-xl">Staff sign in</h1>
      <form onSubmit={submit} className="space-y-3">
        <input name="email" type="email" required autoComplete="username" placeholder="Email" className="w-full rounded-theme border border-border bg-bg px-3 py-2" />
        <input name="password" type="password" required autoComplete="current-password" placeholder="Password" className="w-full rounded-theme border border-border bg-bg px-3 py-2" />
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <button disabled={busy} className="w-full rounded-theme bg-primary py-2 text-primary-fg disabled:opacity-60">{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </main>
  );
}
