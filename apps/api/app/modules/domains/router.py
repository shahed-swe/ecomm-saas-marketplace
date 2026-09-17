import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, text, update
from sqlalchemy.exc import IntegrityError

from app.core import audit
from app.core.deps import Tenant, TenantDB, require_tenant_staff
from app.core.errors import Conflict, NotFound
from app.core.repository import TenantScopedRepository
from app.core.tenancy import invalidate_host, normalise_host
from app.modules.billing.service import require_quota
from app.modules.domains import service
from app.modules.platform.models import Domain

router = APIRouter(prefix="/api/v1/admin/domains", tags=["admin:domains"])
internal = APIRouter(prefix="/internal", include_in_schema=False)


class DomainRepository(TenantScopedRepository[Domain]):
    model = Domain


class DomainIn(BaseModel):
    host: str = Field(min_length=4, max_length=253)


class DomainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    host: str
    kind: str
    status: str
    is_primary: bool


class DomainWithInstructions(DomainOut):
    dns: dict


def _out(d: Domain) -> DomainOut:
    return DomainOut(
        id=str(d.id), host=d.host, kind=d.kind, status=d.status, is_primary=d.is_primary
    )


@router.get("", response_model=list[DomainOut])
async def list_domains(
    tenant: Tenant, db: TenantDB, _=Depends(require_tenant_staff("settings.read"))
):
    return [_out(d) for d in await DomainRepository(db, tenant.id).list()]


@router.post("", response_model=DomainWithInstructions, status_code=201)
async def add_domain(
    body: DomainIn,
    request: Request,
    tenant: Tenant,
    db: TenantDB,
    actor=Depends(require_tenant_staff("settings.write")),
):
    settings = request.app.state.settings
    host = normalise_host(body.host)
    root = settings.platform_root_domain
    if host == root or host.endswith("." + root) or "." not in host or re.match(r"^[\d.]+$", host):
        raise Conflict("Use a domain you own")
    used = (
        await db.execute(
            text("SELECT count(*) FROM domains WHERE tenant_id = :t AND kind = 'custom'"),
            {"t": tenant.id},
        )
    ).scalar()
    await require_quota(db, tenant.id, "custom_domains", used)
    d = DomainRepository(db, tenant.id).add(Domain(host=host, kind="custom", status="pending"))
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise Conflict("Domain unavailable") from exc  # never reveals which store holds it
    await db.refresh(d)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="domain.add",
        entity="domain",
        entity_id=d.id,
        data={"host": host},
        request=request,
    )
    return DomainWithInstructions(
        **_out(d).model_dump(), dns=service.instructions(host, d.verification_token, root)
    )


@router.post("/{domain_id}/verify", response_model=DomainOut)
async def verify_domain(
    domain_id: str,
    request: Request,
    tenant: Tenant,
    db: TenantDB,
    _=Depends(require_tenant_staff("settings.write")),
):
    d = await DomainRepository(db, tenant.id).get(domain_id)
    if d is None or d.kind != "custom":
        raise NotFound("Not found")
    settings = request.app.state.settings
    records = await request.app.state.dns_lookup(d.host)
    ok = service.check_records(
        records,
        token=d.verification_token,
        root_domain=settings.platform_root_domain,
        edge_ips=settings.edge_ips,
    )
    d.last_checked_at = func.now()
    if ok:
        d.status = "active"
        d.verified_at = func.now()
    await db.flush()
    await db.refresh(d)
    await invalidate_host(request.app.state.redis, d.host)
    return _out(d)


@router.post("/{domain_id}/primary", response_model=DomainOut)
async def make_primary(
    domain_id: str,
    request: Request,
    tenant: Tenant,
    db: TenantDB,
    _=Depends(require_tenant_staff("settings.write")),
):
    repo = DomainRepository(db, tenant.id)
    d = await repo.get(domain_id)
    if d is None:
        raise NotFound("Not found")
    if d.status != "active":
        raise Conflict("Verify the domain first")
    hosts = [x.host for x in await repo.list()]
    await db.execute(
        update(Domain)
        .where(Domain.tenant_id == repo.tenant_id, Domain.is_primary)
        .values(is_primary=False)
    )
    await db.flush()
    d.is_primary = True
    await db.flush()
    await invalidate_host(request.app.state.redis, *hosts)
    return _out(d)


@internal.get("/tls/ask")
async def tls_ask(request: Request, domain: str = Query(min_length=1, max_length=253)):
    """Caddy on-demand TLS gate: 200 only for active domains of live tenants."""
    try:
        host = normalise_host(domain)
    except NotFound:
        return Response(status_code=404)
    async with request.app.state.db.engine.connect() as conn:
        allowed = (await conn.execute(text("SELECT tls_host_allowed(:h)"), {"h": host})).scalar()
    return Response(status_code=200 if allowed else 404)
