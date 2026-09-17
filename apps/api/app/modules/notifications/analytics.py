"""Per-tenant analytics destinations: GA4 Measurement Protocol and Meta Conversions API.

Two things this deliberately does:

* **Server-side purchase events.** Browser pixels lose a third of conversions to ad blockers and
  iOS; the purchase that matters is posted from here, where it cannot be blocked, and deduplicated
  against the browser event by `event_id`.
* **PII is hashed, never sent.** Meta wants a customer identifier; it gets SHA-256 of a normalised
  email or phone, which is what its API asks for and all it is entitled to.
"""

import hashlib
import json
import time
from decimal import Decimal

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import Encryptor
from app.core.logging import log

GA4_ENDPOINT = "https://www.google-analytics.com/mp/collect"
META_ENDPOINT = "https://graph.facebook.com/v21.0/{pixel_id}/events"


def _context(tenant_id: str, provider: str) -> str:
    return f"analytics:{tenant_id}:{provider}"


def encrypt_secret(settings, tenant_id: str, provider: str, secret: str) -> str:
    return Encryptor(settings.data_encryption_key).encrypt(
        {"secret": secret}, context=_context(tenant_id, provider)
    )


def decrypt_secret(settings, tenant_id: str, provider: str, ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    return Encryptor(settings.data_encryption_key).decrypt(
        ciphertext, context=_context(tenant_id, provider)
    )["secret"]


def hashed(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


async def destinations(db: AsyncSession, tenant_id: str) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    "SELECT * FROM analytics_destinations WHERE tenant_id = :t AND status = 'active'"
                ),
                {"t": tenant_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def queue_purchase(
    db: AsyncSession,
    tenant_id: str,
    *,
    order_id,
    number: str,
    value: Decimal,
    items: list[dict],
    email: str | None,
    phone: str | None,
) -> int:
    """Queued inside the same transaction as the order, so a purchase event cannot be lost."""
    queued = 0
    for destination in await destinations(db, tenant_id):
        if not destination["server_side"]:
            continue
        payload = {
            "number": number,
            "value": str(value),
            "items": items,
            "email_hash": hashed(email),
            "phone_hash": hashed(phone),
        }
        res = await db.execute(
            text(
                """INSERT INTO analytics_events (tenant_id, provider, name, ref_type, ref_id, payload)
                   VALUES (:t, :p, 'purchase', 'order', :o, CAST(:d AS jsonb))
                   ON CONFLICT DO NOTHING RETURNING id"""
            ),
            {
                "t": tenant_id,
                "p": destination["provider"],
                "o": order_id,
                "d": json.dumps(payload, default=str),
            },
        )
        queued += 1 if res.first() else 0
    return queued


async def flush(
    db: AsyncSession, settings, tenant_id: str, *, limit: int = 100, client=None
) -> dict:
    """Post queued events to each destination. A provider outage leaves the row queued, not lost."""
    rows = (
        (
            await db.execute(
                text(
                    """SELECT e.*, d.public_id, d.secret_ciphertext FROM analytics_events e
                       JOIN analytics_destinations d ON d.tenant_id = e.tenant_id AND d.provider = e.provider
                       WHERE e.tenant_id = :t AND e.status = 'queued' AND d.status = 'active'
                       ORDER BY e.created_at LIMIT :l"""
                ),
                {"t": tenant_id, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    sent = failed = 0
    async with client or httpx.AsyncClient(timeout=10) as http:
        for row in rows:
            try:
                secret = decrypt_secret(
                    settings, tenant_id, row["provider"], row["secret_ciphertext"]
                )
                if row["provider"] == "ga4":
                    await _post_ga4(http, row, secret)
                else:
                    await _post_meta(http, row, secret)
                status, error = "sent", None
                sent += 1
            except Exception as exc:  # noqa: BLE001 - provider errors are data, not crashes
                status, error = "failed", str(exc)[:500]
                failed += 1
                log.warning("analytics_failed", provider=row["provider"], error=error[:120])
            await db.execute(
                text(
                    """UPDATE analytics_events SET status = :s, error = :e,
                              sent_at = CASE WHEN :s = 'sent' THEN now() ELSE NULL END
                       WHERE tenant_id = :t AND id = :i"""
                ),
                {"s": status, "e": error, "t": tenant_id, "i": row["id"]},
            )
    return {"sent": sent, "failed": failed, "pending": max(len(rows) - sent - failed, 0)}


async def _post_ga4(http, row, secret: str | None) -> None:
    payload = row["payload"]
    r = await http.post(
        GA4_ENDPOINT,
        params={"measurement_id": row["public_id"], "api_secret": secret or ""},
        json={
            "client_id": str(row["ref_id"]),
            "events": [
                {
                    "name": "purchase",
                    "params": {
                        "transaction_id": payload.get("number"),
                        "value": float(payload.get("value", 0)),
                        "currency": "BDT",
                        "items": payload.get("items", []),
                    },
                }
            ],
        },
    )
    if r.status_code >= 300:
        raise RuntimeError(f"GA4 rejected the event ({r.status_code})")


async def _post_meta(http, row, secret: str | None) -> None:
    payload = row["payload"]
    user_data = {
        k: [v]
        for k, v in (("em", payload.get("email_hash")), ("ph", payload.get("phone_hash")))
        if v
    }
    r = await http.post(
        META_ENDPOINT.format(pixel_id=row["public_id"]),
        params={"access_token": secret or ""},
        json={
            "data": [
                {
                    "event_name": "Purchase",
                    "event_time": int(time.time()),
                    "event_id": payload.get("number"),  # dedupes against the browser pixel
                    "action_source": "website",
                    "user_data": user_data,
                    "custom_data": {
                        "currency": "BDT",
                        "value": float(payload.get("value", 0)),
                        "order_id": payload.get("number"),
                    },
                }
            ]
        },
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Meta rejected the event ({r.status_code})")
