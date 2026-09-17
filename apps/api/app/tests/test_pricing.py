from decimal import Decimal as D

import pytest

from app.modules.checkout.pricing import (
    Coupon,
    Group,
    Line,
    ShippingRate,
    apply_coupon,
    apply_vat,
    group_total,
    money,
    prorate,
    shipping_fee,
)


def line(price, qty=1, weight=500, vat="0"):
    return Line("v", "p", "vendor", "c", "t", "sku", {}, D(price), D(price), qty, weight, D(vat))


def test_prorate_sums_exactly_and_is_stable():
    parts = prorate(D("100.00"), [D("33.33"), D("33.33"), D("33.34")])
    assert sum(parts) == D("100.00") and len(parts) == 3
    assert prorate(D("0.01"), [D("1"), D("1")]) in ([D("0.01"), D("0.00")], [D("0.00"), D("0.01")])
    assert sum(prorate(D("10.00"), [D("7"), D("3"), D("1")])) == D("10.00")


def test_percent_coupon_with_cap_and_min():
    g = Group("vendor", [line("1000", 2), line("499.99")])
    apply_coupon(g, Coupon("c", "EID10", "percent", D("10"), D("500"), D("200")))
    assert g.discount == D("200.00")  # 10% of 2499.99 = 250.00, capped at 200
    assert [ln.discount for ln in g.lines] == [D("160.00"), D("40.00")]
    small = Group("vendor", [line("100")])
    with pytest.raises(ValueError):
        apply_coupon(small, Coupon("c", "EID10", "percent", D("10"), D("500"), None))


def test_fixed_coupon_never_exceeds_subtotal():
    g = Group("vendor", [line("80")])
    apply_coupon(g, Coupon("c", "FLAT100", "fixed", D("100"), D("0"), None))
    assert g.discount == D("80.00") and g.lines[0].total == D("0.00")


def test_shipping_weight_tiers_and_free_over_after_discount():
    g = Group("vendor", [line("600", qty=2, weight=900)])  # 1800 g
    rate = ShippingRate(D("60"), 1000, D("20"), D("1200"), "tenant")
    shipping_fee(g, rate)
    assert (
        g.shipping_fee == D("80.00")
        and g.shipping_waived == D("80.00")
        and g.shipping_waiver_funded_by == "tenant"
    )
    g2 = Group("vendor", [line("600", qty=2, weight=900)])
    apply_coupon(g2, Coupon("c", "X", "fixed", D("100"), D("0"), None))  # 1100 < 1200 → no waiver
    shipping_fee(g2, rate)
    assert g2.shipping_waived == D("0.00")


def test_vat_inclusive_vs_exclusive():
    inc = Group("vendor", [line("1150", vat="0.15")])
    apply_vat(inc, "inclusive")
    assert inc.vat == D("150.00") and group_total(inc, "inclusive") == D("1150.00")
    exc = Group("vendor", [line("1000", vat="0.15")])
    apply_vat(exc, "exclusive")
    assert exc.vat == D("150.00") and group_total(exc, "exclusive") == D("1150.00")


def test_money_half_up():
    assert money("0.005") == D("0.01") and money("2.675") == D("2.68")
