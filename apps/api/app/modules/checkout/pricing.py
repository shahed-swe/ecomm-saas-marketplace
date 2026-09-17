"""Pure pricing maths (no I/O) so every rule is unit-testable: campaign price, vendor coupon with
largest-remainder proration, shipping with free-over funded by vendor or tenant, VAT inclusive or
exclusive per line, all half-up to 0.01 BDT (ADR 0004, 0005)."""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Line:
    variant_id: str
    product_id: str
    vendor_id: str
    category_id: str
    title: str
    sku: str
    options: dict
    list_price: Decimal
    unit_price: Decimal  # campaign price when active
    qty: int
    weight_grams: int
    vat_rate: Decimal
    campaign_product_id: str | None = None
    discount: Decimal = ZERO
    vat_amount: Decimal = ZERO

    @property
    def gross(self) -> Decimal:
        return money(self.unit_price * self.qty)

    @property
    def total(self) -> Decimal:
        return self.gross - self.discount


@dataclass
class Coupon:
    id: str
    code: str
    kind: str  # percent | fixed
    value: Decimal
    min_subtotal: Decimal
    max_discount: Decimal | None


@dataclass
class ShippingRate:
    base_fee: Decimal
    base_weight_grams: int
    per_extra_kg: Decimal
    free_over: Decimal | None
    free_funded_by: str


@dataclass
class Group:
    vendor_id: str
    lines: list[Line]
    coupon: Coupon | None = None
    coupon_error: str | None = None
    shipping_fee: Decimal = ZERO
    shipping_waived: Decimal = ZERO
    shipping_waiver_funded_by: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def subtotal(self) -> Decimal:
        return sum((ln.gross for ln in self.lines), ZERO)

    @property
    def discount(self) -> Decimal:
        return sum((ln.discount for ln in self.lines), ZERO)

    @property
    def vat(self) -> Decimal:
        return sum((ln.vat_amount for ln in self.lines), ZERO)

    @property
    def weight(self) -> int:
        return sum(ln.weight_grams * ln.qty for ln in self.lines)


def prorate(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split `amount` across lines by weight; largest remainder so the parts sum exactly."""
    total = sum(weights)
    if amount <= 0 or total <= 0:
        return [ZERO for _ in weights]
    cents = int((amount / CENT).to_integral_value())
    raw = [Decimal(cents) * w / total for w in weights]
    base = [int(r) for r in raw]
    remainder = cents - sum(base)
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - base[i], -i), reverse=True)
    for i in order[:remainder]:
        base[i] += 1
    return [Decimal(b) * CENT for b in base]


def coupon_discount(group: Group, coupon: Coupon) -> Decimal:
    subtotal = group.subtotal
    if subtotal < coupon.min_subtotal:
        raise ValueError(
            f"Spend at least ৳{coupon.min_subtotal} with this shop to use {coupon.code}"
        )
    d = money(subtotal * coupon.value / 100) if coupon.kind == "percent" else money(coupon.value)
    if coupon.max_discount is not None:
        d = min(d, coupon.max_discount)
    return min(d, subtotal)


def apply_coupon(group: Group, coupon: Coupon) -> None:
    amount = coupon_discount(group, coupon)
    for ln, part in zip(
        group.lines, prorate(amount, [ln.gross for ln in group.lines]), strict=True
    ):
        ln.discount = part
    group.coupon = coupon


def shipping_fee(group: Group, rate: ShippingRate | None) -> None:
    if rate is None:
        group.shipping_fee = ZERO
        return
    extra_kg = max(0, group.weight - rate.base_weight_grams)
    kgs = -(-extra_kg // 1000)  # ceil
    fee = money(rate.base_fee + rate.per_extra_kg * kgs)
    group.shipping_fee = fee
    if rate.free_over is not None and group.subtotal - group.discount >= rate.free_over:
        group.shipping_waived = fee
        group.shipping_waiver_funded_by = rate.free_funded_by


def apply_vat(group: Group, pricing: str) -> None:
    for ln in group.lines:
        base = ln.total
        if ln.vat_rate <= 0 or base <= 0:
            ln.vat_amount = ZERO
        elif pricing == "inclusive":
            ln.vat_amount = money(base * ln.vat_rate / (1 + ln.vat_rate))
        else:
            ln.vat_amount = money(base * ln.vat_rate)


def group_total(group: Group, pricing: str) -> Decimal:
    t = group.subtotal - group.discount + group.shipping_fee - group.shipping_waived
    return t + (group.vat if pricing == "exclusive" else ZERO)
