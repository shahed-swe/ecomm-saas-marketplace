import { headers } from "next/headers";
import type { paths } from "@ecomm/api-contract";

const API = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

export type StoreInfo = paths["/api/v1/store"]["get"]["responses"]["200"]["content"]["application/json"];

/**
 * Server-side fetch that forwards the buyer's Host so the API resolves the same tenant
 * (architecture §3.1 rule 1). Never called from the browser with a client-chosen host.
 */
export async function apiFetch(path: string, init: RequestInit & { tags?: string[] } = {}) {
  const h = await headers();
  const host = h.get("x-forwarded-host") ?? h.get("host") ?? "";
  const { tags, ...rest } = init;
  return fetch(`${API}${path}`, {
    ...rest,
    headers: { ...(rest.headers ?? {}), "x-forwarded-host": host, "x-request-id": h.get("x-request-id") ?? "" },
    next: tags ? { tags, revalidate: 60 } : undefined,
  });
}

export async function getStore(): Promise<StoreInfo | null> {
  try {
    const r = await apiFetch("/api/v1/store");
    return r.ok ? ((await r.json()) as StoreInfo) : null;
  } catch {
    return null;
  }
}

export async function getPublishedTheme(): Promise<import("./theme").PublishedTheme | null> {
  try {
    const r = await apiFetch("/api/v1/theme");
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}
