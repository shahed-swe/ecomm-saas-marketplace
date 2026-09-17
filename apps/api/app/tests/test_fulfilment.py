"""Phase 12: the parcel, and the cash it carries.

A courier is the only source of truth about where a parcel is, and delivery is what turns a COD
order into money owed to the vendor. So these tests are about believing couriers exactly as much
as they deserve: idempotent events, no backwards transitions, unknown words parked for a human,
and a settlement statement that is matched, never massaged.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal as D

from sqlalchemy import text

from app.modules.fulfilment import service
from app.modules.fulfilment.couriers import Booking, CourierError, CourierEvent
from app.tests.test_checkout import ADDRESS, _buyer, _market, _place, _vendor
from app.tests.test_payments import _gateway

CREDS = {
    "steadfast": {"api_key": "sf-key-9931", "secret_key": "sf-secret"},
    "redx": {"access_token": "redx-token-4412", "pickup_store_id": "9"},
    "pathao": {
        "client_id": "pathao-id-7788",
        "client_secret": "pathao-secret",
        "username": "ops@example.com",
        "password": "hunter2",
        "store_id": "51",
    },
}


class FakeCourier:
    """Records what it was asked to do and says only what the test tells it to say."""

    name = "steadfast"

    def __init__(self):
        self.booked: list[dict] = []
        self.cancelled: list[str] = []
        self.healthy = True
        self.refuse_booking = False
        self.next_status = "picked_up"

    async def quote(self, *, credentials, to, weight_grams, cod_amount):
        return D("70")

    async def book(self, *, credentials, pickup_ref, to, reference, weight_grams, cod_amount, note):
        if self.refuse_booking:
            raise CourierError("no rider in this area")
        cid = f"CN{uuid.uuid4().hex[:8].upper()}"
        self.booked.append(
            {"consignment_id": cid, "reference": reference, "cod": cod_amount, "to": to}
        )
        return Booking(
            consignment_id=cid,
            tracking_code=cid,
            tracking_url=f"https://track.example/{cid}",
            delivery_fee=D("70"),
        )

    async def cancel(self, *, credentials, consignment_id):
        self.cancelled.append(consignment_id)

    async def track(self, *, credentials, consignment_id):
        return CourierEvent(
            event_id=f"poll:{consignment_id}:{self.next_status}",
            consignment_id=consignment_id,
            raw_status=self.next_status,
            status=self.next_status,
            occurred_at=datetime.now(UTC),
            payload={"polled": True},
        )

    async def label(self, *, credentials, consignment_id):
        raise CourierError("no label API")

    def parse_webhook(self, *, credentials, body, form, headers):
        from app.modules.fulfilment.couriers.base import normalise
        from app.modules.fulfilment.couriers.steadfast import STATUS

        cid = form.get("consignment_id")
        raw = str(form.get("status", ""))
        if not cid:
            raise CourierError("webhook without consignment_id")
        return CourierEvent(
            event_id=str(form.get("notification_id") or f"{cid}:{raw}"),
            consignment_id=str(cid),
            raw_status=raw,
            status=normalise(STATUS, raw),
            payload=dict(form),
        )

    async def fetch_settlements(self, *, credentials, since):
        return []

    async def health(self, *, credentials):
        if not self.healthy:
            raise CourierError("courier credentials rejected")


async def _courier(app):
    fake = FakeCourier()
    app.state.couriers = {"steadfast": fake, "pathao": fake, "redx": fake}
    return fake


async def _public_id(app, tenant_id: str) -> str:
    async with app.state.platform_db.engine.begin() as conn:
        return await conn.scalar(
            text("SELECT public_id FROM tenants WHERE id = :t"), {"t": tenant_id}
        )


async def _configure_courier(client, staff, courier="steadfast"):
    r = await client.put(
        "/api/v1/admin/courier-accounts",
        headers=staff,
        json={"courier": courier, "credentials": CREDS[courier], "pickup_ref": "store-1"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _shop(client, app, platform_headers, slug):
    t, staff, cat = await _market(client, app, platform_headers)
    v = await _vendor(client, app, t, staff, slug, cat, price="1000", stock=5, weight=600)
    return t, staff, v


async def _cod_order(client, app, t, v, qty=1):
    h = await _buyer(client, app, t, phone_verified=True)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": qty}
    )
    addr = (await client.post("/api/v1/me/addresses", headers=h, json=ADDRESS)).json()
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    r = await _place(client, h, q, address_id=addr["id"], payment_method="cod")
    assert r.status_code == 201, r.text
    sub = (await client.get("/api/v1/vendor/orders", headers=v["h"])).json()[0]
    return h, r.json(), sub


async def _webhook(client, app, t, cid, status, **extra):
    return await client.post(
        f"/webhooks/courier/steadfast/{await _public_id(app, t['id'])}",
        data={"consignment_id": cid, "status": status, **extra},
    )


# ------------------------------------------------------------- booking and the courier's word
async def test_cod_parcel_from_packing_to_delivered_money(client, app, platform_headers):
    await _gateway(app)
    fake = await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, "ship-alpha")
    await _configure_courier(client, staff)
    h, order, sub = await _cod_order(client, app, t, v, qty=2)

    assert (await client.post(f"/api/v1/vendor/orders/{sub['id']}/ready", headers=v["h"])).json()[
        "status"
    ] == "processing"
    booked = await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    assert booked.status_code == 200, booked.text
    shipment = booked.json()
    assert shipment["courier"] == "steadfast" and D(shipment["cod_amount"]) > 0
    assert fake.booked[0]["reference"] == sub["number"]
    # the courier is told to collect exactly the shipment total, never the whole order
    assert fake.booked[0]["cod"] == D(sub["total"])
    assert (await client.get("/api/v1/vendor/orders", headers=v["h"])).json()[0][
        "status"
    ] == "ready_to_ship"
    # booking twice is refused
    assert (
        await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    ).status_code == 409

    cid = shipment["consignment_id"]
    detail = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    assert detail["variants"][0]["stock_on_hand"] == 5  # COD stock is still on the shelf

    assert (await _webhook(client, app, t, cid, "in_transit")).json()["status"] == "in_transit"
    assert (await client.get("/api/v1/vendor/orders", headers=v["h"])).json()[0][
        "status"
    ] == "shipped"
    detail = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    # the courier has the goods: COD stock leaves the shelf here, not at payment time
    assert detail["variants"][0]["stock_on_hand"] == 3 and detail["variants"][0]["available"] == 3

    # the buyer can follow the parcel
    tracking = (await client.get(f"/api/v1/me/orders/{order['number']}/tracking", headers=h)).json()
    assert tracking[0]["status"] == "in_transit" and tracking[0]["tracking_code"] == cid

    # delivery is what turns COD into money owed
    assert (await _webhook(client, app, t, cid, "delivered")).json()["status"] == "delivered"
    assert (await client.get("/api/v1/vendor/cod-receivables", headers=v["h"])).json()[0][
        "status"
    ] == "collected"
    assert (await client.get("/api/v1/admin/payments", headers=staff)).json()[0]["status"] == "paid"
    assert (await client.get(f"/api/v1/me/orders/{order['number']}", headers=h)).json()[
        "status"
    ] == "delivered"


async def test_events_are_idempotent_and_never_run_backwards(client, app, platform_headers):
    await _gateway(app)
    await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, "ship-beta")
    await _configure_courier(client, staff)
    h, order, sub = await _cod_order(client, app, t, v)
    cid = (
        await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    ).json()["consignment_id"]

    first = await _webhook(client, app, t, cid, "delivered", notification_id="n1")
    assert first.json()["status"] == "delivered"
    # the same notification again
    assert (await _webhook(client, app, t, cid, "delivered", notification_id="n1")).json()[
        "status"
    ] == "duplicate"
    # a late "in transit" cannot un-deliver a parcel
    late = await _webhook(client, app, t, cid, "in_transit", notification_id="n2")
    assert late.json()["status"] == "stale" and late.json()["current"] == "delivered"
    assert (await client.get("/api/v1/vendor/orders", headers=v["h"])).json()[0][
        "status"
    ] == "delivered"
    # collecting COD twice does not pay twice
    payments_rows = (await client.get("/api/v1/admin/payments", headers=staff)).json()
    assert len(payments_rows) == 1 and payments_rows[0]["status"] == "paid"


async def test_unknown_courier_word_parks_the_parcel_for_a_human(client, app, platform_headers):
    await _gateway(app)
    await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, "ship-gamma")
    await _configure_courier(client, staff)
    h, order, sub = await _cod_order(client, app, t, v)
    cid = (
        await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    ).json()["consignment_id"]

    r = await _webhook(client, app, t, cid, "unknown", notification_id="u1")
    assert r.json()["status"] == "needs_attention"
    flagged = (
        await client.get("/api/v1/admin/shipments?needs_attention=true", headers=staff)
    ).json()
    assert len(flagged) == 1 and "unmapped" in flagged[0]["attention_reason"]
    assert flagged[0]["status"] == "booked"  # nothing moved on a word we do not understand
    detail = (await client.get(f"/api/v1/admin/shipments/{flagged[0]['id']}", headers=staff)).json()
    assert (
        detail["events"][-1]["raw_status"] == "unknown" and detail["events"][-1]["status"] is None
    )
    cleared = await client.post(
        f"/api/v1/admin/shipments/{flagged[0]['id']}/resolve",
        headers=staff,
        json={"note": "called the hub"},
    )
    assert cleared.json()["needs_attention"] is False
    assert (
        await client.get("/api/v1/admin/shipments?needs_attention=true", headers=staff)
    ).json() == []


async def test_return_to_merchant_puts_stock_back(client, app, platform_headers):
    await _gateway(app)
    await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, "ship-delta")
    await _configure_courier(client, staff)
    h, order, sub = await _cod_order(client, app, t, v, qty=2)
    cid = (
        await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    ).json()["consignment_id"]
    await _webhook(client, app, t, cid, "in_transit", notification_id="r1")
    detail = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    assert detail["variants"][0]["stock_on_hand"] == 3

    assert (await _webhook(client, app, t, cid, "returned", notification_id="r2")).json()[
        "status"
    ] == "returned"
    detail = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    assert detail["variants"][0]["stock_on_hand"] == 5  # back on the shelf
    assert (await client.get("/api/v1/vendor/cod-receivables", headers=v["h"])).json()[0][
        "status"
    ] == "cancelled"
    async with app.state.platform_db.engine.begin() as conn:
        reasons = [
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT reason FROM inventory_movements WHERE tenant_id = :t "
                        "ORDER BY created_at, reason"
                    ),
                    {"t": t["id"]},
                )
            ).all()
        ]
    assert reasons[-2:] == ["order", "return"]  # both movements are on the record


async def test_courier_rules_choose_the_carrier(client, app, platform_headers):
    await _gateway(app)
    await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, "ship-epsilon")
    await _configure_courier(client, staff, "steadfast")
    await _configure_courier(client, staff, "redx")
    rules = await client.put(
        "/api/v1/admin/courier-rules",
        headers=staff,
        json={
            "rules": [
                {"courier": "redx", "zones": ["inside_dhaka"], "max_cod_amount": 500},
                {"courier": "steadfast", "districts": []},
            ]
        },
    )
    assert [r["courier"] for r in rules.json()] == ["redx", "steadfast"]
    # a ৳1000+ COD parcel exceeds the redx rule, so it falls through to steadfast
    h, order, sub = await _cod_order(client, app, t, v)
    booked = (
        await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    ).json()
    assert booked["courier"] == "steadfast"

    # an explicit choice by the vendor always wins
    h2, order2, sub2 = await _cod_order(client, app, t, v)
    picked = (
        await client.post(
            f"/api/v1/vendor/orders/{sub2['id']}/ship", headers=v["h"], json={"courier": "redx"}
        )
    ).json()
    assert picked["courier"] == "redx"


async def test_cancelling_a_booking_returns_the_shipment_to_the_vendor(
    client, app, platform_headers
):
    await _gateway(app)
    fake = await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, "ship-zeta")
    await _configure_courier(client, staff)
    h, order, sub = await _cod_order(client, app, t, v)
    shipment = (
        await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    ).json()
    r = await client.post(f"/api/v1/vendor/shipments/{shipment['id']}/cancel", headers=v["h"])
    assert r.json()["status"] == "cancelled" and fake.cancelled == [shipment["consignment_id"]]
    assert (await client.get("/api/v1/vendor/orders", headers=v["h"])).json()[0][
        "status"
    ] == "processing"
    # and it can be booked again with another courier
    again = await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    assert again.status_code == 200 and again.json()["consignment_id"] != shipment["consignment_id"]
    # a delivered parcel cannot be cancelled
    await _webhook(
        client, app, t, again.json()["consignment_id"], "delivered", notification_id="c1"
    )
    assert (
        await client.post(f"/api/v1/vendor/shipments/{again.json()['id']}/cancel", headers=v["h"])
    ).status_code == 409


async def test_polling_catches_the_webhook_that_never_arrived(client, app, platform_headers):
    await _gateway(app)
    fake = await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, "ship-eta")
    await _configure_courier(client, staff)
    h, order, sub = await _cod_order(client, app, t, v)
    await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE shipments SET last_event_at = now() - interval '2 hours' WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    fake.next_status = "delivered"
    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            out = await service.poll_open_shipments(
                s, app.state.settings, t["id"], stale_minutes=30, overrides={"steadfast": fake}
            )
            assert out == {"polled": 1, "advanced": 1, "errors": 0}
            stuck = await service.flag_stuck_shipments(s, t["id"], sla_hours=1)
    assert stuck == 0  # a delivered parcel is never "stuck"
    assert (await client.get("/api/v1/vendor/cod-receivables", headers=v["h"])).json()[0][
        "status"
    ] == "collected"


async def test_stuck_parcel_is_flagged_after_the_sla(client, app, platform_headers):
    await _gateway(app)
    await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, "ship-theta")
    await _configure_courier(client, staff)
    h, order, sub = await _cod_order(client, app, t, v)
    await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE shipments SET last_event_at = now() - interval '5 days', "
                "booked_at = now() - interval '5 days' WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            assert await service.flag_stuck_shipments(s, t["id"], sla_hours=72) == 1
            assert await service.flag_stuck_shipments(s, t["id"], sla_hours=72) == 0  # not twice
    flagged = (
        await client.get("/api/v1/admin/shipments?needs_attention=true", headers=staff)
    ).json()
    assert flagged[0]["attention_reason"] == "no courier update within SLA"


# ----------------------------------------------------------------------------- COD settlement
async def test_settlement_statement_is_matched_not_massaged(client, app, platform_headers):
    await _gateway(app)
    await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, "ship-iota")
    await _configure_courier(client, staff)
    parcels = []
    for _ in range(2):
        h, order, sub = await _cod_order(client, app, t, v)
        shipment = (
            await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
        ).json()
        await _webhook(
            client,
            app,
            t,
            shipment["consignment_id"],
            "delivered",
            notification_id=uuid.uuid4().hex,
        )
        parcels.append(shipment)
    good, wrong = parcels
    csv = (
        "consignment_id,cod_amount,delivery_fee\n"
        f"{good['consignment_id']},{good['cod_amount']},70\n"
        f"{wrong['consignment_id']},{D(wrong['cod_amount']) - D('100')},70\n"
        "CN-NOT-OURS,500,70\n"
    ).encode()
    r = await client.post(
        "/api/v1/admin/courier-settlements",
        headers=staff,
        data={"courier": "steadfast", "statement_ref": "SF-2026-01"},
        files={"file": ("statement.csv", csv, "text/csv")},
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert (out["matched"], out["mismatch"], out["unmatched"]) == (1, 1, 1)

    receivables = (await client.get("/api/v1/vendor/cod-receivables", headers=v["h"])).json()
    settled = [x for x in receivables if x["status"] == "settled"]
    collected = [x for x in receivables if x["status"] == "collected"]
    assert len(settled) == 1 and len(collected) == 1  # the short-paid one is NOT settled
    lines = (
        await client.get(
            "/api/v1/admin/courier-settlement-lines?statement_ref=SF-2026-01", headers=staff
        )
    ).json()
    assert {x["status"] for x in lines} == {"matched", "mismatch", "unmatched"}
    mismatch = next(x for x in lines if x["status"] == "mismatch")
    assert "statement says" in mismatch["note"]
    assert (await client.get("/api/v1/admin/shipments?needs_attention=true", headers=staff)).json()[
        0
    ]["id"] == wrong["id"]
    # the same statement cannot be imported twice
    again = await client.post(
        "/api/v1/admin/courier-settlements",
        headers=staff,
        data={"courier": "steadfast", "statement_ref": "SF-2026-01"},
        files={"file": ("statement.csv", csv, "text/csv")},
    )
    assert again.status_code == 409


# ------------------------------------------------------------------------------ tenant safety
async def test_courier_webhook_cannot_cross_tenants(client, app, platform_headers):
    await _gateway(app)
    await _courier(app)
    a_t, a_staff, a_v = await _shop(client, app, platform_headers, "ship-kappa")
    b_t, b_staff, _ = await _shop(client, app, platform_headers, "ship-lambda")
    await _configure_courier(client, a_staff)
    await _configure_courier(client, b_staff)
    h, order, sub = await _cod_order(client, app, a_t, a_v)
    cid = (
        await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=a_v["h"], json={})
    ).json()["consignment_id"]

    r = await _webhook(client, app, b_t, cid, "delivered", notification_id="x1")
    assert r.json()["status"] == "unknown_consignment"
    assert (await client.get("/api/v1/vendor/orders", headers=a_v["h"])).json()[0][
        "status"
    ] == "ready_to_ship"
    assert (
        await client.post("/webhooks/courier/steadfast/deadbeef", data={"consignment_id": cid})
    ).status_code == 404


async def test_courier_credentials_are_encrypted_and_masked(client, app, platform_headers):
    await _gateway(app)
    fake = await _courier(app)
    t, staff, _ = await _shop(client, app, platform_headers, "ship-mu")
    view = await _configure_courier(client, staff)
    assert view["status"] == "healthy" and view["key_hint"] == "••••9931"
    async with app.state.platform_db.engine.begin() as conn:
        stored = await conn.scalar(
            text("SELECT credentials_ciphertext FROM courier_accounts WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    assert "sf-secret" not in stored and "sf-key-9931" not in stored
    fake.healthy = False
    bad = await client.post(
        "/api/v1/admin/courier-accounts/health", headers=staff, json={"courier": "steadfast"}
    )
    assert bad.json()["status"] == "failing" and "rejected" in bad.json()["last_error"]
    assert (
        await client.put(
            "/api/v1/admin/courier-accounts",
            headers=staff,
            json={"courier": "pathao", "credentials": {"client_id": "x"}},
        )
    ).status_code == 422
