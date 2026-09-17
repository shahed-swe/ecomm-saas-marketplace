import re
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.logging import log

_RID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Request id propagation (web -> api -> worker) and access log."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _RID.match(incoming) else uuid.uuid4().hex
        request.state.request_id = rid
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers every response carries, whatever route produced it.

    The API serves JSON, not pages, so its own CSP can be absolute: nothing may be loaded, framed
    or embedded from an API response. The storefront's CSP is a separate, much longer conversation
    and lives with the web app; this is the part that is simply always true.
    """

    def __init__(self, app, *, hsts: bool = True):
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("x-content-type-options", "nosniff")
        headers.setdefault("x-frame-options", "DENY")
        headers.setdefault("referrer-policy", "no-referrer")
        headers.setdefault("cross-origin-opener-policy", "same-origin")
        headers.setdefault("cross-origin-resource-policy", "same-site")
        headers.setdefault("permissions-policy", "geolocation=(), camera=(), microphone=()")
        headers.setdefault(
            "content-security-policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        # A signed URL or an error must never sit in a shared cache.
        headers.setdefault("cache-control", "no-store")
        if self.hsts:
            headers.setdefault(
                "strict-transport-security", "max-age=63072000; includeSubDomains; preload"
            )
        return response
