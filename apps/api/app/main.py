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
from app.modules.health.router import router as health_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.env)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = Database(settings)
        app.state.redis = create_redis(settings)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.db.dispose()

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
    app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
    return app


app = create_app()
