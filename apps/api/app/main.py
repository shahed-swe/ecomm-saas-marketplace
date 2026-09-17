from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware, metrics_endpoint
from app.core.middleware import RequestContextMiddleware
from app.core.redis import create_redis
from app.core.storage import build_storage
from app.modules.billing import router as billing
from app.modules.domains.router import internal as internal_router
from app.modules.domains.router import router as domains_router
from app.modules.domains.service import real_dns_lookup
from app.modules.health.router import router as health_router
from app.modules.identity import router as identity
from app.modules.identity import staff_router
from app.modules.identity.sms import ConsoleSms
from app.modules.platform.router import router as platform_router
from app.modules.settings.router import router as settings_router
from app.modules.store.router import router as store_router
from app.modules.theme import router as theme
from app.modules.vendors.router import admin_router as admin_vendors_router
from app.modules.vendors.router import vendor_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.env)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = Database(settings)
        app.state.platform_db = Database(settings, platform=True)
        app.state.storage = getattr(app.state, "storage", None) or build_storage(settings)
        app.state.sms = getattr(app.state, "sms", None) or ConsoleSms()
        app.state.dns_lookup = getattr(app.state, "dns_lookup", None) or real_dns_lookup
        app.state.redis = create_redis(settings)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.db.dispose()
            await app.state.platform_db.dispose()

    app = FastAPI(
        title="ecomm SaaS marketplace API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "production" else None,
    )
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    install_error_handlers(app)
    app.include_router(health_router)
    for r in (
        identity.router,
        identity.me_router,
        identity.platform_auth,
        staff_router.admin,
        staff_router.vendor,
        store_router,
        theme.public,
        theme.admin,
        settings_router,
        billing.admin,
        billing.platform,
        vendor_router,
        admin_vendors_router,
        domains_router,
        platform_router,
        internal_router,
    ):
        app.include_router(r)
    app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
    return app


app = create_app()
