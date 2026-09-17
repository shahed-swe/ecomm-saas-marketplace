"""Custom domains (architecture §4): TXT ownership proof + CNAME/A to our edge."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.resolver

VERIFY_PREFIX = "_ecomm-verify"


@dataclass
class DnsRecords:
    txt: list[str]
    cname: list[str]
    a: list[str]


DnsLookup = Callable[[str], Awaitable[DnsRecords]]


async def _query(name: str, rtype: str) -> list[str]:
    try:
        answer = await dns.asyncresolver.resolve(name, rtype, lifetime=5)
    except (
        dns.resolver.NXDOMAIN,
        dns.resolver.NoAnswer,
        dns.exception.Timeout,
        dns.resolver.NoNameservers,
    ):
        return []
    if rtype == "TXT":
        return [b"".join(r.strings).decode() for r in answer]
    return [r.to_text().rstrip(".").lower() for r in answer]


async def real_dns_lookup(host: str) -> DnsRecords:
    return DnsRecords(
        txt=await _query(f"{VERIFY_PREFIX}.{host}", "TXT"),
        cname=await _query(host, "CNAME"),
        a=await _query(host, "A"),
    )


def check_records(
    records: DnsRecords, *, token: str, root_domain: str, edge_ips: list[str]
) -> bool:
    owns = token in records.txt
    root = root_domain.lower()
    points = any(c == root or c.endswith("." + root) for c in records.cname) or (
        bool(edge_ips) and bool(records.a) and set(records.a) <= set(edge_ips)
    )
    return owns and points


def instructions(host: str, token: str, root_domain: str) -> dict:
    return {
        "records": [
            {"type": "TXT", "name": f"{VERIFY_PREFIX}.{host}", "value": token},
            {"type": "CNAME", "name": host, "value": f"stores.{root_domain}"},
        ]
    }


async def recheck_active_domains(
    session, lookup: DnsLookup, *, root_domain: str, edge_ips: list[str], redis=None
) -> list[str]:
    """Platform job: custom domains whose DNS no longer proves ownership become inactive,
    so Caddy's `ask` stops issuing certificates for them (architecture §15)."""
    from sqlalchemy import func, select

    from app.modules.platform.models import Domain

    rows = (
        (
            await session.execute(
                select(Domain).where(Domain.kind == "custom", Domain.status == "active")
            )
        )
        .scalars()
        .all()
    )
    deactivated = []
    for d in rows:
        records = await lookup(d.host)
        d.last_checked_at = func.now()
        if not check_records(
            records, token=d.verification_token, root_domain=root_domain, edge_ips=edge_ips
        ):
            d.status = "inactive"
            d.is_primary = False
            deactivated.append(d.host)
    await session.flush()
    if redis is not None and deactivated:
        await redis.delete(*(f"host:{h}" for h in deactivated))
    return deactivated
