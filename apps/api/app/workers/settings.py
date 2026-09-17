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


@platform_job
async def escalate_returns(ctx: dict) -> int:
    """QC nobody did in time stops being the buyer's problem and becomes the tenant's."""
    from sqlalchemy import text

    from app.core.tenancy import scope_session
    from app.modules.returns.service import escalate_overdue_qc

    total = 0
    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        tenant_ids = [
            str(r[0])
            for r in (
                await session.execute(
                    text(
                        """SELECT DISTINCT tenant_id FROM return_requests
                           WHERE status IN ('received','picked_up') AND NOT escalated
                             AND qc_due_at IS NOT NULL AND qc_due_at < now() LIMIT 200"""
                    )
                )
            ).all()
        ]
    for tenant_id in tenant_ids:
        async with ctx["db"].sessionmaker() as session, session.begin():
            await scope_session(session, tenant_id)
            total += await escalate_overdue_qc(session, tenant_id)
    if total:
        log.info("returns_escalated", count=total)
    return total


@platform_job
async def finance_reconcile(ctx: dict) -> dict:
    """Nightly, per tenant: does the ledger still agree with the systems it describes?"""
    from sqlalchemy import text

    from app.core.tenancy import scope_session
    from app.modules.ledger.reconcile import reconcile

    drifted = []
    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        tenant_ids = [
            str(r[0])
            for r in (
                await session.execute(
                    text("SELECT DISTINCT tenant_id FROM ledger_entries LIMIT 500")
                )
            ).all()
        ]
    for tenant_id in tenant_ids:
        async with ctx["db"].sessionmaker() as session, session.begin():
            await scope_session(session, tenant_id)
            result = await reconcile(session, tenant_id)
        if not result["clean"]:
            drifted.append(
                {"tenant_id": tenant_id, "checks": [c for c in result["checks"] if c["drift"]]}
            )
            log.error("ledger_drift", tenant_id=tenant_id, drift_count=result["drift_count"])
    log.info("finance_reconciled", tenants=len(tenant_ids), drifted=len(drifted))
    return {"tenants": len(tenant_ids), "drifted": len(drifted)}


@platform_job
async def payout_runs(ctx: dict) -> dict:
    """Prepare each tenant's payout batch on its own schedule. Preparing is not paying: a batch is
    a draft until a human with the right permission approves it (ADR 0009)."""
    from datetime import date

    from sqlalchemy import text

    from app.core.tenancy import scope_session
    from app.modules.ledger import payouts

    built = 0
    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        tenants = (
            await session.execute(
                text(
                    """SELECT t.id, ts.payout_schedule FROM tenants t
                       JOIN tenant_settings ts ON ts.tenant_id = t.id
                       WHERE t.status IN ('trial','active') LIMIT 500"""
                )
            )
        ).all()
    for tenant_id, schedule in tenants:
        period_end = payouts.period_end_for(schedule, date.today())
        async with ctx["db"].sessionmaker() as session, session.begin():
            await scope_session(session, str(tenant_id))
            try:
                result = await payouts.build_batch(
                    session, str(tenant_id), period_end=period_end, actor_id="scheduler"
                )
            except payouts.PayoutError:
                continue  # this period already has a batch
        if result["line_count"]:
            built += 1
    log.info("payout_runs", tenants=len(tenants), batches=built)
    return {"tenants": len(tenants), "batches": built}


@platform_job
async def trust_sweep(ctx: dict) -> dict:
    """Disputes nobody answered escalate; vendor scorecards are recomputed from the week's facts."""
    from sqlalchemy import text

    from app.core.tenancy import scope_session
    from app.modules.trust.service import escalate_overdue_disputes, score_all

    escalated, scored = 0, 0
    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        tenant_ids = [
            str(r[0])
            for r in (
                await session.execute(
                    text("SELECT id FROM tenants WHERE status IN ('trial','active') LIMIT 500")
                )
            ).all()
        ]
    for tenant_id in tenant_ids:
        async with ctx["db"].sessionmaker() as session, session.begin():
            await scope_session(session, tenant_id)
            escalated += await escalate_overdue_disputes(session, tenant_id)
            scored += (await score_all(session, tenant_id))["scored"]
    log.info("trust_sweep", tenants=len(tenant_ids), escalated=escalated, scored=scored)
    return {"tenants": len(tenant_ids), "escalated": escalated, "scored": scored}


@platform_job
async def marketing_sweep(ctx: dict) -> dict:
    """Abandoned-cart nudges and queued analytics events, per tenant, on the hour."""
    from sqlalchemy import text

    from app.core.tenancy import scope_session
    from app.modules.notifications import analytics
    from app.modules.notifications.campaigns import nudge_abandoned_carts

    settings = get_settings()
    totals = {"tenants": 0, "nudged": 0, "events_sent": 0}
    async with ctx["platform_db"].sessionmaker() as session, session.begin():
        tenant_ids = [
            str(r[0])
            for r in (
                await session.execute(
                    text("SELECT id FROM tenants WHERE status IN ('trial','active') LIMIT 500")
                )
            ).all()
        ]
    for tenant_id in tenant_ids:
        async with ctx["db"].sessionmaker() as session, session.begin():
            await scope_session(session, tenant_id)
            nudges = await nudge_abandoned_carts(session, ctx["app_state"], tenant_id)
            flushed = await analytics.flush(session, settings, tenant_id)
        totals["tenants"] += 1
        totals["nudged"] += nudges["nudged"]
        totals["events_sent"] += flushed["sent"]
    log.info("marketing_sweep", **totals)
    return totals


class WorkerAppState:
    """The little bit of app state the notification service needs outside a request."""

    def __init__(self, settings):
        from app.modules.identity.sms import ConsoleSms
        from app.modules.notifications.channels import ConsoleEmail, ConsolePush

        self.settings = settings
        self.sms = ConsoleSms()
        self.push = ConsolePush()
        self.email = ConsoleEmail()


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.env)
    ctx["db"] = Database(settings)
    ctx["platform_db"] = Database(settings, platform=True)
    ctx["app_state"] = WorkerAppState(settings)


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
        escalate_returns,
        finance_reconcile,
        payout_runs,
        trust_sweep,
        marketing_sweep,
    ]
    cron_jobs = [
        cron(recheck_domains, hour={0, 6, 12, 18}, minute=17),
        cron(billing_cycle, hour={20}, minute=5),  # 02:05 Asia/Dhaka
        cron(expire_unpaid_orders, second={0}),  # every minute
        cron(reconcile_payments, minute={3, 18, 33, 48}),  # four sweeps an hour
        cron(courier_sweep, minute={8, 23, 38, 53}),
        cron(escalate_returns, minute={40}),  # hourly
        cron(finance_reconcile, hour={21}, minute={30}),  # 03:30 Asia/Dhaka
        cron(payout_runs, hour={22}, minute={15}),  # 04:15 Asia/Dhaka
        cron(trust_sweep, hour={1, 13}, minute={50}),
        cron(marketing_sweep, minute={25}),  # hourly
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 300
