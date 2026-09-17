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


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.env)
    ctx["db"] = Database(settings)
    ctx["platform_db"] = Database(settings, platform=True)


async def shutdown(ctx: dict) -> None:
    await ctx["db"].dispose()
    await ctx["platform_db"].dispose()


class WorkerSettings:
    functions = [heartbeat, recheck_domains]
    cron_jobs = [cron(recheck_domains, hour={0, 6, 12, 18}, minute=17)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 300
