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
