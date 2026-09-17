"""One gateway interface, many providers (ADR 0006).

Rules every implementation must honour:
 * the server verifies before an order is paid — a callback only tells us to go and check;
 * `parse_callback` extracts a provider event id so replays are a no-op;
 * credentials belong to the tenant and are passed in per call (never module state).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


@dataclass
class CreatedPayment:
    provider_ref: str
    redirect_url: str
    raw: dict = field(default_factory=dict)


@dataclass
class VerifiedPayment:
    status: str  # paid | pending | failed | cancelled
    amount: Decimal | None = None
    payer_ref: str | None = None
    fee: Decimal = Decimal("0")
    failure_reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class CallbackEvent:
    event_id: str
    provider_ref: str
    kind: str
    payload: dict


class GatewayError(Exception):
    pass


class PaymentGateway(Protocol):
    name: str

    async def create(
        self,
        *,
        credentials: dict,
        amount: Decimal,
        order_number: str,
        return_url: str,
        callback_url: str,
        buyer_phone: str | None,
    ) -> CreatedPayment: ...

    async def verify(self, *, credentials: dict, provider_ref: str) -> VerifiedPayment: ...

    def parse_callback(
        self, *, credentials: dict, body: bytes, form: dict, headers: dict
    ) -> CallbackEvent: ...

    async def refund(
        self, *, credentials: dict, provider_ref: str, amount: Decimal, reason: str
    ) -> dict: ...

    async def health(self, *, credentials: dict) -> None: ...
