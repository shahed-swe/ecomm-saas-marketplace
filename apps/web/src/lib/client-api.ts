"use client";
// Browser API client for the staff console. Same-origin /api/v1 (host decides the tenant).
// The access token lives in memory + sessionStorage (tab-scoped); the refresh token is an httpOnly cookie.
let memoryToken: string | null = null;

export function setToken(token: string | null) {
  memoryToken = token;
  try { token ? sessionStorage.setItem("at", token) : sessionStorage.removeItem("at"); } catch { /* private mode */ }
}

function token(): string | null {
  if (memoryToken) return memoryToken;
  try { memoryToken = sessionStorage.getItem("at"); } catch { /* ignore */ }
  return memoryToken;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: string) { super(detail); }
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const doFetch = () => fetch(`/api/v1${path}`, {
    ...init,
    credentials: "same-origin",
    headers: {
      ...(init.json !== undefined ? { "content-type": "application/json" } : {}),
      ...(token() ? { authorization: `Bearer ${token()}` } : {}),
      ...(init.headers ?? {}),
    },
    body: init.json !== undefined ? JSON.stringify(init.json) : init.body,
  });
  let r = await doFetch();
  if (r.status === 401 && token()) {
    const rr = await fetch("/api/v1/auth/refresh", { method: "POST", credentials: "same-origin" });
    if (rr.ok) { setToken((await rr.json()).access_token); r = await doFetch(); }
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new ApiError(r.status, body.detail ?? r.statusText);
  }
  return r.status === 204 ? (undefined as T) : r.json();
}
