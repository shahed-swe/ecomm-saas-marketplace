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
        return await process_asset(session, build_storage(settings), build_private_storage(settings),
                                   tenant_id=tenant_id, asset_id=asset_id)


@tenant_job
async def import_products(ctx: dict, *, tenant_id: str, job_id: str) -> dict:
    from app.core.storage import build_private_storage
    from app.core.tenancy import scope_session
    from app.modules.catalog.imports import run_import
    from app.modules.catalog.revalidate import RecordingRevalidator, WebRevalidator

    settings = get_settings()
    reval = WebRevalidator(settings.web_revalidate_url, settings.jwt_secret) if settings.web_revalidate_url \
        else RecordingRevalidator()
    async with ctx["db"].sessionmaker() as session, session.begin():
        await scope_session(session, tenant_id)
        return await run_import(session, build_private_storage(settings), reval, tenant_id=tenant_id, job_id=job_id)


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.env)
    ctx["db"] = Database(settings)
    ctx["platform_db"] = Database(settings, platform=True)


async def shutdown(ctx: dict) -> None:
    await ctx["db"].dispose()
    await ctx["platform_db"].dispose()


class WorkerSettings:
    functions = [heartbeat, recheck_domains, billing_cycle, process_media, import_products]
    cron_jobs = [
        cron(recheck_domains, hour={0, 6, 12, 18}, minute=17),
        cron(billing_cycle, hour={20}, minute=5),  # 02:05 Asia/Dhaka
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 300
