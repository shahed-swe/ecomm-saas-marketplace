"""Every tenant-owned key carries the tenant prefix (architecture §3.1 rule 6)."""

import re

_PART = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def tkey(tenant_id: str, *parts: str) -> str:
    if not tenant_id:
        raise ValueError("tenant_id required for tenant cache key")
    for p in parts:
        if not _PART.match(p):
            raise ValueError(f"invalid key part: {p!r}")
    return ":".join(("t", str(tenant_id), *parts))


def object_key(tenant_id: str, *parts: str) -> str:
    if not tenant_id:
        raise ValueError("tenant_id required for object key")
    return "/".join(("t", str(tenant_id), *parts))


def isr_tag(tenant_id: str, *parts: str) -> str:
    return tkey(tenant_id, *parts)
