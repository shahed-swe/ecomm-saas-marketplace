import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.modules.checkout.service import expire_unpaid
from app.tests.conftest import PASSWORD, jpeg_bytes, login, make_tenant, set_password

ADDRESS = {
    "recipient_name": "Rumana",
    "phone": "01712345699",
    "district_code": "dhaka",
    "upazila": "Uttara",
    "address_line": "Road 7, House 12",
    "is_default": True,
}


def _staff(t):
    return {"authorization": f"Bearer {t['staff_token']}", "host": t["primary_host"]}


async def _market(client, app, platform_headers, **settings):
    t = await make_tenant(
        client,
        app,
        platform_headers,
        slug=f"ck{uuid.uuid4().hex[:6]}",
        name="Checkout shop",
        store_mode="multi",
    )
    staff = _staff(t)
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_settings SET moderation_mode='none' WHERE tenant_id=:t"),
            {"t": t["id"]},
        )
    cat = (
        await client.post(
            "/api/v1/admin/catalog/categories",
            headers=staff,
            json={"slug": "all", "name_en": "All"},
        )
    ).json()
    await client.put(
        "/api/v1/admin/shipping-rates",
        headers=staff,
        json={
            "zone": "inside_dhaka",
            "base_fee": "60",
            "base_weight_grams": 1000,
            "per_extra_kg": "20",
            "free_over": "3000",
            "free_funded_by": "tenant",
        },
    )
    if settings:
        await client.patch("/api/v1/admin/checkout-settings", headers=staff, json=settings)
    return t, staff, cat["id"]


async def _vendor(
    client, app, t, staff, slug: str, cat_id: str, *, price="1000", stock=5, weight=800, sku=None
):
    email = f"{slug}@example.com"
    v = await client.post(
        "/api/v1/admin/vendors",
        headers=staff,
        json={"slug": slug, "display_name": slug.title(), "owner_email": email},
    )
    vid = v.json()["id"]
    await set_password(app, email, t["id"])
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(text("UPDATE vendors SET status='approved' WHERE id=:v"), {"v": vid})
    tok = await login(client, t["primary_host"], email, "vendor", vid)
    client.cookies.clear()
    vh = {"authorization": f"Bearer {tok}", "host": t["primary_host"]}
    asset = (
        await client.post(
            "/api/v1/vendor/media",
            headers=vh,
            files={
                "file": ("p.jpg", jpeg_bytes(color=(len(slug) * 9 % 255, 20, 20)), "image/jpeg")
            },
        )
    ).json()
    p = await client.post(
        "/api/v1/vendor/products",
        headers=vh,
        json={
            "slug": f"item-{slug}",
            "title_en": f"Item {slug}",
            "category_id": cat_id,
            "weight_grams": weight,
            "variants": [{"sku": sku or f"SKU-{slug.upper()}", "price": price, "stock": stock}],
        },
    )
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    await client.post(
        f"/api/v1/vendor/products/{pid}/media", headers=vh, json={"asset_id": asset["id"]}
    )
    await client.patch(f"/api/v1/vendor/products/{pid}", headers=vh, json={"status": "active"})
    return {"id": vid, "h": vh, "product_id": pid, "variant_id": p.json()["variants"][0]["id"]}


async def _buyer(client, app, t, *, phone_verified=False, email=None):
    email = email or f"b{uuid.uuid4().hex[:6]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        headers={"host": t["primary_host"]},
        json={"email": email, "password": PASSWORD},
    )
    client.cookies.clear()
    h = {"authorization": f"Bearer {r.json()['access_token']}", "host": t["primary_host"]}
    if phone_verified:
        async with app.state.platform_db.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE users SET phone = :p, phone_verified_at = now() WHERE tenant_id = :t AND email = :e"
                ),
                {"p": f"+88017{uuid.uuid4().int % 100000000:08d}", "t": t["id"], "e": email},
            )
    return h


async def _place(client, h, quote, **kw):
    body = {"payment_method": "bkash", "expected_total": quote["grand_total"], **kw}
    return await client.post(
        "/api/v1/checkout/place", headers={**h, "idempotency-key": uuid.uuid4().hex}, json=body
    )


async def test_grouped_cart_shipping_vat_and_order_tree(client, app, platform_headers):
    t, staff, cat = await _market(
        client, app, platform_headers, vat_pricing="inclusive", default_vat_rate="0.05"
    )
    a = await _vendor(client, app, t, staff, "alpha-shop", cat, price="1000", weight=800)
    b = await _vendor(client, app, t, staff, "beta-shop", cat, price="2500", weight=1500)
    h = await _buyer(client, app, t)
    for v, qty in ((a["variant_id"], 2), (b["variant_id"], 1)):
        assert (
            await client.post("/api/v1/cart/items", headers=h, json={"variant_id": v, "qty": qty})
        ).status_code == 201
    addr = (await client.post("/api/v1/me/addresses", headers=h, json=ADDRESS)).json()
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    groups = {g["vendor_id"]: g for g in q["groups"]}
    assert len(groups) == 2
    # vendor A: 2 × 1000, 1.6 kg → 60 + 20 (one extra kg); free over 3000 applies to A (2000? no) → check
    assert groups[a["id"]]["subtotal"] == "2000.00" and groups[a["id"]]["shipping_fee"] == "80.00"
    assert groups[a["id"]]["shipping_waived"] == "0.00"
    # vendor B: 2500, 1.5 kg → 60 + 20; no free shipping under 3000
    assert groups[b["id"]]["shipping_fee"] == "80.00"
    assert q["items_subtotal"] == "4500.00" and q["shipping_total"] == "160.00"
    assert q["vat_total"] == "214.29"  # 5% inclusive of 4500
    assert q["grand_total"] == "4660.00" and "cod" in q["payment_methods"]

    r = await _place(client, h, q, address_id=addr["id"])
    assert r.status_code == 201, r.text
    order = (await client.get(f"/api/v1/me/orders/{r.json()['number']}", headers=h)).json()
    assert order["status"] == "pending_payment" and len(order["shipments"]) == 2
    assert sum(D(s["total"]) for s in order["shipments"]) == D(order["grand_total"])
    # each vendor sees only its own shipment
    a_orders = (await client.get("/api/v1/vendor/orders", headers=a["h"])).json()
    b_orders = (await client.get("/api/v1/vendor/orders", headers=b["h"])).json()
    assert (
        len(a_orders) == 1 and len(b_orders) == 1 and a_orders[0]["number"] != b_orders[0]["number"]
    )
    assert (
        await client.get(f"/api/v1/vendor/orders/{b_orders[0]['id']}", headers=a["h"])
    ).status_code == 404
    # cart is emptied and stock reserved
    assert (await client.get("/api/v1/cart", headers=h)).json()["items"] == []
    detail = (await client.get(f"/api/v1/vendor/products/{a['product_id']}", headers=a["h"])).json()
    assert detail["variants"][0]["stock_on_hand"] == 5 and detail["variants"][0]["available"] == 3


async def test_free_shipping_threshold_and_coupon_interaction(client, app, platform_headers):
    t, staff, cat = await _market(client, app, platform_headers)
    v = await _vendor(client, app, t, staff, "gamma-shop", cat, price="3200", stock=5, weight=500)
    await client.post(
        "/api/v1/vendor/coupons",
        headers=v["h"],
        json={"code": "SAVE300", "kind": "fixed", "value": "300", "min_subtotal": "1000"},
    )
    h = await _buyer(client, app, t)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 1}
    )
    plain = (
        await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})
    ).json()
    assert plain["groups"][0]["shipping_waived"] == "60.00"  # 3200 ≥ 3000
    with_coupon = (
        await client.post(
            "/api/v1/cart/quote",
            headers=h,
            json={"district_code": "dhaka", "coupons": {v["id"]: "save300"}},
        )
    ).json()
    g = with_coupon["groups"][0]
    assert (
        g["discount"] == "300.00" and g["shipping_waived"] == "0.00"
    )  # 2900 after discount → fee returns
    assert with_coupon["grand_total"] == "2960.00"
    bad = (
        await client.post(
            "/api/v1/cart/quote",
            headers=h,
            json={"district_code": "dhaka", "coupons": {v["id"]: "NOPE"}},
        )
    ).json()
    assert bad["groups"][0]["coupon_error"] == "Coupon not valid for this shop"


async def test_campaign_price_and_cap(client, app, platform_headers):
    t, staff, cat = await _market(client, app, platform_headers)
    v = await _vendor(client, app, t, staff, "delta-shop", cat, price="2000", stock=10)
    c = (
        await client.post(
            "/api/v1/admin/campaigns",
            headers=staff,
            json={
                "slug": "flash",
                "name": "Flash",
                "starts_at": "2026-01-01T00:00:00Z",
                "ends_at": "2030-01-01T00:00:00Z",
                "max_discount_percent": "50",
            },
        )
    ).json()
    too_cheap = await client.post(
        f"/api/v1/vendor/campaigns/{c['id']}/products",
        headers=v["h"],
        json={"variant_id": v["variant_id"], "campaign_price": "500", "stock_cap": 2},
    )
    assert too_cheap.status_code == 422
    assert (
        await client.post(
            f"/api/v1/vendor/campaigns/{c['id']}/products",
            headers=v["h"],
            json={"variant_id": v["variant_id"], "campaign_price": "1200", "stock_cap": 2},
        )
    ).status_code == 201
    h = await _buyer(client, app, t)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 2}
    )
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    assert (
        q["groups"][0]["items"][0]["unit_price"] == "1200.00"
        and q["groups"][0]["items"][0]["list_price"] == "2000.00"
    )
    assert (await _place(client, h, q, address=ADDRESS)).status_code == 201
    # cap of 2 is used up: the next buyer pays full price
    h2 = await _buyer(client, app, t)
    await client.post(
        "/api/v1/cart/items", headers=h2, json={"variant_id": v["variant_id"], "qty": 1}
    )
    q2 = (
        await client.post("/api/v1/cart/quote", headers=h2, json={"district_code": "dhaka"})
    ).json()
    assert q2["groups"][0]["items"][0]["unit_price"] == "2000.00"


async def test_no_oversell_under_concurrency_and_no_deadlock(client, app, platform_headers):
    t, staff, cat = await _market(client, app, platform_headers)
    a = await _vendor(client, app, t, staff, "stock-a", cat, price="500", stock=3)
    b = await _vendor(client, app, t, staff, "stock-b", cat, price="500", stock=3)
    buyers = []
    for _ in range(6):
        h = await _buyer(client, app, t)
        # carts with the two variants in OPPOSITE order: deterministic locking must prevent deadlocks
        order = (
            [a["variant_id"], b["variant_id"]]
            if len(buyers) % 2 == 0
            else [b["variant_id"], a["variant_id"]]
        )
        for vid in order:
            await client.post("/api/v1/cart/items", headers=h, json={"variant_id": vid, "qty": 2})
        q = (
            await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})
        ).json()
        buyers.append((h, q))

    async def attempt(h, q):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/api/v1/checkout/place",
                headers={**h, "idempotency-key": uuid.uuid4().hex},
                json={
                    "address": ADDRESS,
                    "payment_method": "bkash",
                    "expected_total": q["grand_total"],
                },
            )
            return r.status_code

    codes = await asyncio.gather(*(attempt(h, q) for h, q in buyers))
    assert sorted(codes).count(201) == 1  # only one cart of 2+2 fits in stock 3+3
    assert all(c in (201, 409) for c in codes), codes
    async with app.state.platform_db.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """SELECT stock_on_hand, stock_reserved FROM product_variants WHERE id = ANY(CAST(:ids AS uuid[]))"""
                ),
                {"ids": [a["variant_id"], b["variant_id"]]},
            )
        ).all()
    assert all(r.stock_reserved <= r.stock_on_hand for r in rows)
    assert sum(r.stock_reserved for r in rows) == 4


async def test_idempotent_placement_and_total_check(client, app, platform_headers):
    t, staff, cat = await _market(client, app, platform_headers)
    v = await _vendor(client, app, t, staff, "idem-shop", cat, price="900", stock=9)
    h = await _buyer(client, app, t)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 1}
    )
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    key = uuid.uuid4().hex
    body = {"address": ADDRESS, "payment_method": "bkash", "expected_total": q["grand_total"]}
    first = await client.post(
        "/api/v1/checkout/place", headers={**h, "idempotency-key": key}, json=body
    )
    second = await client.post(
        "/api/v1/checkout/place", headers={**h, "idempotency-key": key}, json=body
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["number"] == second.json()["number"] and second.json()["replayed"] is True
    assert len((await client.get("/api/v1/me/orders", headers=h)).json()) == 1
    # stale total is refused
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 1}
    )
    stale = await client.post(
        "/api/v1/checkout/place",
        headers={**h, "idempotency-key": uuid.uuid4().hex},
        json={"address": ADDRESS, "payment_method": "bkash", "expected_total": "1.00"},
    )
    assert stale.status_code == 409 and stale.json()["title"] == "price_changed"


async def test_cod_rules(client, app, platform_headers):
    t, staff, cat = await _market(client, app, platform_headers, cod_max_order="1500")
    v = await _vendor(client, app, t, staff, "cod-shop", cat, price="1000", stock=9)
    h = await _buyer(client, app, t, phone_verified=True)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 2}
    )
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    assert "cod" not in q["payment_methods"]  # 2060 > cod_max_order
    r = await client.post(
        "/api/v1/checkout/place",
        headers={**h, "idempotency-key": uuid.uuid4().hex},
        json={"address": ADDRESS, "payment_method": "cod", "expected_total": q["grand_total"]},
    )
    assert r.status_code == 409 and r.json()["title"] == "payment_unavailable"
    await client.patch("/api/v1/cart/items/" + v["variant_id"], headers=h, json={"qty": 1})
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    assert "cod" in q["payment_methods"]
    ok = await client.post(
        "/api/v1/checkout/place",
        headers={**h, "idempotency-key": uuid.uuid4().hex},
        json={"address": ADDRESS, "payment_method": "cod", "expected_total": q["grand_total"]},
    )
    assert ok.status_code == 201
    # COD orders are confirmed immediately (no payment window); prepaid ones are not
    detail = (await client.get(f"/api/v1/me/orders/{ok.json()['number']}", headers=h)).json()
    assert detail["status"] == "processing" and detail["payment_due_at"] is None
    # an unverified phone cannot use COD
    h2 = await _buyer(client, app, t)
    await client.post(
        "/api/v1/cart/items", headers=h2, json={"variant_id": v["variant_id"], "qty": 1}
    )
    q2 = (
        await client.post("/api/v1/cart/quote", headers=h2, json={"district_code": "dhaka"})
    ).json()
    r2 = await client.post(
        "/api/v1/checkout/place",
        headers={**h2, "idempotency-key": uuid.uuid4().hex},
        json={"address": ADDRESS, "payment_method": "cod", "expected_total": q2["grand_total"]},
    )
    assert r2.status_code == 409 and r2.json()["title"] == "phone_unverified"


async def test_unpaid_orders_expire_and_release_stock(client, app, platform_headers):
    t, staff, cat = await _market(client, app, platform_headers, reservation_minutes=5)
    v = await _vendor(client, app, t, staff, "expire-shop", cat, price="700", stock=2)
    h = await _buyer(client, app, t)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 2}
    )
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    placed = await _place(client, h, q, address=ADDRESS)
    assert placed.status_code == 201
    h2 = await _buyer(client, app, t)
    await client.post(
        "/api/v1/cart/items", headers=h2, json={"variant_id": v["variant_id"], "qty": 1}
    )
    q2 = (
        await client.post("/api/v1/cart/quote", headers=h2, json={"district_code": "dhaka"})
    ).json()
    assert q2["issues"][0]["code"] == "insufficient_stock"

    async with app.state.platform_db.sessionmaker() as db, db.begin():
        n = await expire_unpaid(db, now=datetime.now(UTC) + timedelta(minutes=6))
    assert n >= 1
    q3 = (
        await client.post("/api/v1/cart/quote", headers=h2, json={"district_code": "dhaka"})
    ).json()
    assert q3["issues"] == [] and q3["groups"][0]["items"][0]["qty"] == 1
    cancelled = (await client.get(f"/api/v1/me/orders/{placed.json()['number']}", headers=h)).json()
    assert cancelled["status"] == "cancelled"


async def test_guest_tracking_token(client, world):
    A = world["A"]
    host = {"host": A.host}
    r = await client.get(
        "/api/v1/orders/track",
        headers=host,
        params={"number": A.order_number, "token": A.tracking_token},
    )
    assert r.status_code == 200 and r.json()["number"] == A.order_number
    assert "shipping_address" not in r.text and "contact_phone" not in r.text
    assert (
        await client.get(
            "/api/v1/orders/track",
            headers=host,
            params={"number": A.order_number, "token": "x" * 20},
        )
    ).status_code == 404
    # another tenant cannot read it even with the right token
    assert (
        await client.get(
            "/api/v1/orders/track",
            headers={"host": world["B"].host},
            params={"number": A.order_number, "token": A.tracking_token},
        )
    ).status_code == 404


async def test_commission_snapshot_on_sub_orders(client, app, platform_headers):
    t, staff, cat = await _market(client, app, platform_headers)
    v = await _vendor(client, app, t, staff, "commission-shop", cat, price="1000", stock=5)
    await client.post(
        f"/api/v1/admin/vendors/{v['id']}/commission", headers=staff, json={"rate": "0.07"}
    )
    h = await _buyer(client, app, t)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 1}
    )
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    r = await _place(client, h, q, address=ADDRESS)
    async with app.state.platform_db.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT commission_rate, commission_source FROM sub_orders WHERE tenant_id = :t"
                ),
                {"t": t["id"]},
            )
        ).one()
    assert row.commission_rate == D("0.0700") and row.commission_source == "vendor"
    # changing the rate later does not touch the placed order
    await client.post(
        f"/api/v1/admin/vendors/{v['id']}/commission", headers=staff, json={"rate": "0.20"}
    )
    async with app.state.platform_db.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT commission_rate FROM sub_orders WHERE tenant_id = :t"), {"t": t["id"]}
            )
        ).one()
    assert row.commission_rate == D("0.0700")
    assert r.status_code == 201
