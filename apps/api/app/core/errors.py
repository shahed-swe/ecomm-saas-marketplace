from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import log


class AppError(Exception):
    """Domain error rendered as RFC 9457 problem+json.

    Messages must never reveal another tenant's or vendor's data
    (e.g. never "SKU already exists for vendor X").
    """

    status: int = 400
    code: str = "bad_request"

    def __init__(self, detail: str = "", *, status: int | None = None, code: str | None = None):
        super().__init__(detail)
        self.detail = detail
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class NotFound(AppError):
    status = 404
    code = "not_found"


class Forbidden(AppError):
    status = 403
    code = "forbidden"


class Unauthorized(AppError):
    status = 401
    code = "unauthorized"


class Conflict(AppError):
    status = 409
    code = "conflict"


def _problem(request: Request, status: int, code: str, detail: str, **extra) -> JSONResponse:
    body = {
        "type": f"about:blank#{code}",
        "title": code,
        "status": status,
        "detail": detail,
        "request_id": getattr(request.state, "request_id", None),
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return _problem(request, exc.status, exc.code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return _problem(request, 422, "validation_error", "Invalid request", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return _problem(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled_error", path=request.url.path)
        return _problem(request, 500, "internal_error", "Something went wrong")
