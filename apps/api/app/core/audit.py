"""Append-only audit log (architecture §13). Written in the same transaction as the change."""

from typing import Any

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal

_SECRET_KEYS = {"password", "password_hash", "token", "code", "secret", "totp", "account_number"}


def _scrub(data: dict[str, Any]) -> dict[str, Any]:
    return {k: ("[redacted]" if k.lower() in _SECRET_KEYS else v) for k, v in data.items()}


async def record(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor: Principal | None,
    action: str,
    entity: str,
    entity_id: Any = None,
    data: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    import json

    ip = None
    rid = None
    if request is not None:
        ip = request.client.host if request.client else None
        rid = getattr(request.state, "request_id", None)
    await session.execute(
        text(
            """INSERT INTO audit_log (tenant_id, actor_kind, actor_id, vendor_id, action, entity,
                                      entity_id, data, ip, request_id)
               VALUES (:t, :ak, :aid, :vid, :action, :entity, :eid, CAST(:data AS jsonb),
                       CAST(:ip AS inet), :rid)"""
        ),
        {
            "t": str(tenant_id),
            "ak": actor.kind if actor else "system",
            "aid": actor.sub if actor else "system",
            "vid": actor.vid if actor else None,
            "action": action,
            "entity": entity,
            "eid": str(entity_id) if entity_id is not None else None,
            "data": json.dumps(_scrub(data or {}), default=str),
            "ip": ip if ip and ip != "testclient" else None,
            "rid": rid,
        },
    )
