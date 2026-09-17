"""ARQ worker. Every job is tenant-aware (architecture §3.1 rule 5).

Jobs are declared with @tenant_job; enqueueing without tenant_id raises before
anything reaches Redis. Platform-wide jobs use @platform_job explicitly.
"""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.db import Database
from app.core.logging import configure_logging, log

JobFn = Callable[..., Awaitable[Any]]


class MissingTenantError(ValueError):
    pass


def tenant_job(fn: JobFn) -> JobFn:
    @wraps(fn)
    async def wrapper(ctx: dict, *args: Any, tenant_id: str | None = None, **kwargs: Any):
        if not tenant_id:
            raise MissingTenantError(f"{fn.__name__} requires tenant_id")
        structlog.contextvars.bind_contextvars(tenant_id=tenant_id, job=fn.__name__)
        try:
            return await fn(ctx, *args, tenant_id=tenant_id, **kwargs)
        finally:
            structlog.contextvars.unbind_contextvars("tenant_id", "job")

    wrapper.__tenant_job__ = True  # type: ignore[attr-defined]
    return wrapper


def platform_job(fn: JobFn) -> JobFn:
    fn.__platform_job__ = True  # type: ignore[attr-defined]
    return fn


async def enqueue_tenant_job(pool, fn_name: str, *, tenant_id: str, **kwargs: Any):
    if not tenant_id:
        raise MissingTenantError(f"{fn_name} requires tenant_id")
    return await pool.enqueue_job(fn_name, tenant_id=tenant_id, **kwargs)


@platform_job
async def heartbeat(ctx: dict) -> str:
    log.info("worker_heartbeat")
    return "ok"


@platform_job
async def recheck_domains(ctx: dict) -> list[str]:
    from app.modules.domains.service import real_dns_lookup, recheck_active_domains

    settings = get_settings()
    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        return await recheck_active_domains(
            session,
            real_dns_lookup,
            root_domain=settings.platform_root_domain,
            edge_ips=settings.edge_ips,
        )


@platform_job
async def billing_cycle(ctx: dict) -> dict:
    from app.modules.billing.service import run_billing_cycle

    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        return await run_billing_cycle(session)


@tenant_job
async def process_media(ctx: dict, *, tenant_id: str, asset_id: str) -> dict:
    from app.core.storage import build_private_storage, build_storage
    from app.core.tenancy import scope_session
    from app.modules.catalog.media import process_asset

    settings = get_settings()
    async with ctx["db"].sessionmaker() as session, session.begin():
        await scope_session(session, tenant_id)
        return await process_asset(
            session,
            build_storage(settings),
            build_private_storage(settings),
            tenant_id=tenant_id,
            asset_id=asset_id,
        )


@tenant_job
async def import_products(ctx: dict, *, tenant_id: str, job_id: str) -> dict:
    from app.core.storage import build_private_storage
    from app.core.tenancy import scope_session
    from app.modules.catalog.imports import run_import
    from app.modules.catalog.revalidate import RecordingRevalidator, WebRevalidator

    settings = get_settings()
    reval = (
        WebRevalidator(settings.web_revalidate_url, settings.jwt_secret)
        if settings.web_revalidate_url
        else RecordingRevalidator()
    )
    async with ctx["db"].sessionmaker() as session, session.begin():
        await scope_session(session, tenant_id)
        return await run_import(
            session, build_private_storage(settings), reval, tenant_id=tenant_id, job_id=job_id
        )


@platform_job
async def expire_unpaid_orders(ctx: dict) -> int:
    from app.modules.checkout.service import expire_unpaid

    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        return await expire_unpaid(session)


@platform_job
async def reconcile_payments(ctx: dict) -> dict:
    """Every tenant with stale gateway attempts is asked the provider what really happened."""
    from sqlalchemy import text

    from app.core.tenancy import scope_session
    from app.modules.payments.service import reconcile_pending

    settings = get_settings()
    totals = {"tenants": 0, "checked": 0, "paid": 0, "closed": 0, "unresolved": 0}
    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        tenant_ids = [
            str(r[0])
            for r in (
                await session.execute(
                    text(
                        """SELECT DISTINCT tenant_id FROM payments
                           WHERE status = 'pending' AND provider <> 'cod' AND provider_ref IS NOT NULL
                             AND updated_at < now() - interval '10 minutes' LIMIT 200"""
                    )
                )
            ).all()
        ]
    for tenant_id in tenant_ids:
        async with ctx["db"].sessionmaker() as session, session.begin():
            await scope_session(session, tenant_id)
            result = await reconcile_pending(session, settings, tenant_id=tenant_id)
        totals["tenants"] += 1
        for k in ("checked", "paid", "closed", "unresolved"):
            totals[k] += result[k]
    log.info("payments_reconciled", **totals)
    return totals


@platform_job
async def courier_sweep(ctx: dict) -> dict:
    """Webhooks get lost and parcels get forgotten: poll open shipments, flag the stuck ones."""
    from sqlalchemy import text

    from app.core.tenancy import scope_session
    from app.modules.fulfilment.service import flag_stuck_shipments, poll_open_shipments

    settings = get_settings()
    totals = {"tenants": 0, "polled": 0, "advanced": 0, "errors": 0, "flagged": 0}
    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        tenant_ids = [
            str(r[0])
            for r in (
                await session.execute(
                    text(
                        """SELECT DISTINCT tenant_id FROM shipments
                           WHERE status NOT IN ('delivered','returned','cancelled') LIMIT 200"""
                    )
                )
            ).all()
        ]
    for tenant_id in tenant_ids:
        async with ctx["db"].sessionmaker() as session, session.begin():
            await scope_session(session, tenant_id)
            result = await poll_open_shipments(session, settings, tenant_id)
            totals["flagged"] += await flag_stuck_shipments(session, tenant_id)
        totals["tenants"] += 1
        for k in ("polled", "advanced", "errors"):
            totals[k] += result[k]
    log.info("courier_sweep", **totals)
    return totals


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.env)
    ctx["db"] = Database(settings)
    ctx["platform_db"] = Database(settings, platform=True)


async def shutdown(ctx: dict) -> None:
    await ctx["db"].dispose()
    await ctx["platform_db"].dispose()


class WorkerSettings:
    functions = [
        heartbeat,
        recheck_domains,
        billing_cycle,
        process_media,
        import_products,
        expire_unpaid_orders,
        reconcile_payments,
        courier_sweep,
    ]
    cron_jobs = [
        cron(recheck_domains, hour={0, 6, 12, 18}, minute=17),
        cron(billing_cycle, hour={20}, minute=5),  # 02:05 Asia/Dhaka
        cron(expire_unpaid_orders, second={0}),  # every minute
        cron(reconcile_payments, minute={3, 18, 33, 48}),  # four sweeps an hour
        cron(courier_sweep, minute={8, 23, 38, 53}),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 300
