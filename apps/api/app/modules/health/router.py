from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness: process is up. No dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: DB and Redis reachable."""
    checks: dict[str, str] = {}
    db = request.app.state.db
    try:
        async with db.engine.connect() as conn:
            await conn.execute(text("select 1"))
        checks["db"] = "ok"
    except Exception:  # noqa: BLE001
        checks["db"] = "fail"
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception:  # noqa: BLE001
        checks["redis"] = "fail"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503
    )
