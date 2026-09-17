from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI
from fastapi import encoders as encoders_module
from fastapi.encoders import ENCODERS_BY_TYPE as _ENCODERS
from fastapi.encoders import generate_encoders_by_class_tuples as _by_tuples
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware, metrics_endpoint
from app.core.middleware import RequestContextMiddleware
from app.core.redis import create_redis
from app.core.storage import build_private_storage, build_storage
from app.modules.billing import router as billing
from app.modules.catalog import buyer as catalog_buyer
from app.modules.catalog import imports as catalog_imports
from app.modules.catalog import media as catalog_media
from app.modules.catalog import products as catalog_products
from app.modules.catalog import search as catalog_search
from app.modules.catalog import seo as catalog_seo
from app.modules.catalog import taxonomy as catalog_taxonomy
from app.modules.catalog.revalidate import RecordingRevalidator, WebRevalidator
from app.modules.checkout import router as checkout
from app.modules.domains.router import internal as internal_router
from app.modules.domains.router import router as domains_router
from app.modules.domains.service import real_dns_lookup
from app.modules.fulfilment import router as fulfilment
from app.modules.health.router import router as health_router
from app.modules.identity import router as identity
from app.modules.identity import staff_router
from app.modules.identity.sms import ConsoleSms
from app.modules.ledger import router as ledger
from app.modules.notifications import router as notifications
from app.modules.notifications.channels import ConsoleEmail, ConsolePush
from app.modules.payments import router as payments
from app.modules.platform.router import router as platform_router
from app.modules.reporting import router as reporting
from app.modules.returns import router as returns
from app.modules.settings.router import router as settings_router
from app.modules.store.router import router as store_router
from app.modules.theme import router as theme
from app.modules.theme import storefront as theme_storefront
from app.modules.theme import vendor_store
from app.modules.trust import router as trust
from app.modules.vendors import onboarding as vendor_onboarding
from app.modules.vendors.router import admin_router as admin_vendors_router
from app.modules.vendors.router import vendor_router

# Money must never reach a client as a float (0.1 + 0.2 problems in JS). FastAPI's encoder maps
# Decimal -> float by default; make it a string everywhere, for every route.
_ENCODERS[Decimal] = str
encoders_module.encoders_by_class_tuples = _by_tuples(_ENCODERS)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.env)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = Database(settings)
        app.state.platform_db = Database(settings, platform=True)
        app.state.storage = getattr(app.state, "storage", None) or build_storage(settings)
        app.state.private_storage = getattr(
            app.state, "private_storage", None
        ) or build_private_storage(settings)
        if getattr(app.state, "revalidator", None) is None:
            app.state.revalidator = (
                WebRevalidator(settings.web_revalidate_url, settings.jwt_secret)
                if settings.web_revalidate_url
                else RecordingRevalidator()
            )
        # Dev/test process inline (threadpool); production wires ARQ queues in the worker bootstrap.
        app.state.media_queue = getattr(
            app.state, "media_queue", None
        ) or catalog_media.InlineMediaQueue(app)
        app.state.import_queue = getattr(
            app.state, "import_queue", None
        ) or catalog_imports.InlineImportQueue(app)
        app.state.sms = getattr(app.state, "sms", None) or ConsoleSms()
        app.state.push = getattr(app.state, "push", None) or ConsolePush()
        app.state.email = getattr(app.state, "email", None) or ConsoleEmail()
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
        theme_storefront.router,
        vendor_store.vendor,
        vendor_store.public,
        settings_router,
        billing.admin,
        billing.platform,
        vendor_onboarding.public,
        vendor_onboarding.vendor,
        vendor_onboarding.admin,
        vendor_onboarding.internal,
        catalog_taxonomy.admin,
        catalog_taxonomy.public,
        catalog_products.vendor,
        catalog_products.admin,
        catalog_products.public,
        catalog_media.router,
        catalog_imports.router,
        catalog_search.router,
        catalog_search.admin,
        catalog_buyer.router,
        catalog_seo.router,
        checkout.buyer,
        checkout.vendor,
        checkout.admin,
        payments.buyer,
        payments.vendor,
        payments.admin,
        payments.webhooks,
        fulfilment.vendor,
        fulfilment.admin,
        fulfilment.buyer,
        fulfilment.webhooks,
        returns.buyer,
        returns.vendor,
        returns.admin,
        ledger.admin,
        ledger.vendor,
        ledger.buyer,
        trust.buyer,
        trust.vendor,
        trust.admin,
        notifications.buyer,
        notifications.vendor,
        notifications.admin,
        reporting.admin,
        reporting.vendor,
        reporting.platform,
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
