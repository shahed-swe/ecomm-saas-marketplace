"""Access tokens (ADR 0014). Full login flows arrive in Phase 3; the claim contract is fixed here.

Claims: sub, kind, tid (tenant; absent only for platform), vid (vendor staff), roles, exp, iat, jti.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

import jwt

from app.core.config import Settings

Kind = Literal["buyer", "tenant_staff", "vendor_staff", "platform"]
ALGORITHM = "HS256"


@dataclass(frozen=True)
class Principal:
    sub: str
    kind: Kind
    tid: str | None = None
    vid: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)


def create_access_token(settings: Settings, p: Principal, ttl: int | None = None) -> str:
    now = int(time.time())
    claims: dict = {
        "sub": p.sub,
        "kind": p.kind,
        "roles": list(p.roles),
        "iat": now,
        "exp": now + (ttl or settings.jwt_access_ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    if p.tid:
        claims["tid"] = p.tid
    if p.vid:
        claims["vid"] = p.vid
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(settings: Settings, token: str) -> Principal:
    data = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[ALGORITHM],
        options={"require": ["sub", "kind", "exp", "iat"]},
    )
    kind = data["kind"]
    if kind not in ("buyer", "tenant_staff", "vendor_staff", "platform"):
        raise jwt.InvalidTokenError("bad kind")
    if kind != "platform" and not data.get("tid"):
        raise jwt.InvalidTokenError("tid required")
    if kind == "vendor_staff" and not data.get("vid"):
        raise jwt.InvalidTokenError("vid required")
    return Principal(
        sub=data["sub"],
        kind=kind,
        tid=data.get("tid"),
        vid=data.get("vid"),
        roles=tuple(data.get("roles", [])),
    )
