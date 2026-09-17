"""One courier interface, three Bangladeshi couriers (ADR 0007).

The normalised status vocabulary is the contract: every provider word is mapped into it, and an
**unknown word is never guessed** — it returns None so the shipment goes to the ops queue instead
of silently looking delivered.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

STATUSES = (
    "booked",
    "picked_up",
    "in_transit",
    "out_for_delivery",
    "delivered",
    "partial_delivered",
    "failed_attempt",
    "returning",
    "returned",
    "cancelled",
)
# How far along a shipment is. A courier may report an older event after a newer one
# (webhooks arrive out of order); progress never goes backwards except for explicit terminals.
PROGRESS = {s: i for i, s in enumerate(STATUSES)}
TERMINAL = {"delivered", "returned", "cancelled"}


@dataclass
class Address:
    name: str
    phone: str
    district: str
    upazila: str | None
    area: str | None
    line: str


@dataclass
class Booking:
    consignment_id: str
    tracking_code: str | None = None
    tracking_url: str | None = None
    delivery_fee: Decimal | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class CourierEvent:
    event_id: str
    consignment_id: str
    raw_status: str
    status: str | None  # None = unknown to us; the shipment is flagged for a human
    occurred_at: datetime | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class SettlementLine:
    consignment_id: str
    amount: Decimal
    fee: Decimal = Decimal("0")
    paid_at: datetime | None = None


class CourierError(Exception):
    pass


def normalise(mapping: dict[str, str], raw: str | None) -> str | None:
    if not raw:
        return None
    return mapping.get(str(raw).strip().lower().replace(" ", "_"))


class CourierAdapter(Protocol):
    name: str

    async def quote(
        self, *, credentials: dict, to: Address, weight_grams: int, cod_amount: Decimal
    ) -> Decimal | None: ...

    async def book(
        self,
        *,
        credentials: dict,
        pickup_ref: str | None,
        to: Address,
        reference: str,
        weight_grams: int,
        cod_amount: Decimal,
        note: str | None,
    ) -> Booking: ...

    async def cancel(self, *, credentials: dict, consignment_id: str) -> None: ...

    async def track(self, *, credentials: dict, consignment_id: str) -> CourierEvent: ...

    async def label(self, *, credentials: dict, consignment_id: str) -> bytes: ...

    def parse_webhook(
        self, *, credentials: dict, body: bytes, form: dict, headers: dict
    ) -> CourierEvent: ...

    async def fetch_settlements(
        self, *, credentials: dict, since: datetime
    ) -> list[SettlementLine]: ...

    async def health(self, *, credentials: dict) -> None: ...
