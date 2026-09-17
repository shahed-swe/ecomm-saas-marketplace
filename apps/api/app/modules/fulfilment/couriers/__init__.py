"""Courier registry (ADR 0007). `app.state.couriers[name]` swaps a real adapter for a fake."""

from app.modules.fulfilment.couriers.base import (
    PROGRESS,
    STATUSES,
    TERMINAL,
    Address,
    Booking,
    CourierAdapter,
    CourierError,
    CourierEvent,
    SettlementLine,
    normalise,
)
from app.modules.fulfilment.couriers.pathao import PathaoCourier
from app.modules.fulfilment.couriers.redx import RedxCourier
from app.modules.fulfilment.couriers.steadfast import SteadfastCourier

COURIERS = ("pathao", "steadfast", "redx")
_BUILDERS = {"pathao": PathaoCourier, "steadfast": SteadfastCourier, "redx": RedxCourier}


def build_courier(courier: str, mode: str, overrides: dict | None = None) -> CourierAdapter:
    override = (overrides or {}).get(courier)
    if override is not None:
        return override
    try:
        return _BUILDERS[courier](mode)
    except KeyError as exc:  # pragma: no cover - guarded by route validation
        raise CourierError(f"unknown courier {courier}") from exc


__all__ = [
    "COURIERS",
    "PROGRESS",
    "STATUSES",
    "TERMINAL",
    "Address",
    "Booking",
    "CourierAdapter",
    "CourierError",
    "CourierEvent",
    "PathaoCourier",
    "RedxCourier",
    "SettlementLine",
    "SteadfastCourier",
    "build_courier",
    "normalise",
]
