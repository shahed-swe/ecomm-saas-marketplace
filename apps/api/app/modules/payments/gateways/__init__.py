"""Gateway registry (ADR 0006). One interface, two Bangladeshi providers; tests inject fakes."""

from app.modules.payments.gateways.base import (
    CallbackEvent,
    CreatedPayment,
    GatewayError,
    PaymentGateway,
    VerifiedPayment,
)
from app.modules.payments.gateways.bkash import BkashGateway
from app.modules.payments.gateways.sslcommerz import SslCommerzGateway

PROVIDERS = ("bkash", "sslcommerz")
_BUILDERS = {"bkash": BkashGateway, "sslcommerz": SslCommerzGateway}


def build_gateway(provider: str, mode: str, overrides: dict | None = None) -> PaymentGateway:
    """`app.state.payment_gateways[provider]` (or a worker's dict) overrides the real client."""
    override = (overrides or {}).get(provider)
    if override is not None:
        return override
    try:
        return _BUILDERS[provider](mode)
    except KeyError as exc:  # pragma: no cover - guarded by route validation
        raise GatewayError(f"unknown provider {provider}") from exc


__all__ = [
    "PROVIDERS",
    "BkashGateway",
    "CallbackEvent",
    "CreatedPayment",
    "GatewayError",
    "PaymentGateway",
    "SslCommerzGateway",
    "VerifiedPayment",
    "build_gateway",
]
