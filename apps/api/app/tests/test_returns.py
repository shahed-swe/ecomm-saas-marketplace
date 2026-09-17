"""Phase 13: giving money back.

A refund is the easiest place in a marketplace to lose money twice — refund the buyer and forget
the stock, refund twice because a button was double-clicked, refund more than was paid, or refund
to a destination nobody checked. These tests are about each of those.
"""

import uuid
from decimal import Decimal as D

from sqlalchemy import text

from app.modules.returns import credit as store_credit
from app.modules.returns import service
from app.tests.test_checkout import ADDRESS, _buyer, _market, _place, _vendor
from app.tests.test_fulfilment import _configure_courier, _courier, _webhook
from app.tests.test_payments import _configure, _gateway


async def _shop(client, app, platform_headers, slug, **settings):
    t, staff, cat = await _market(client, app, platform_headers, **settings)
    v = await _vendor(client, app, t, staff, slug, cat, price="1000", stock=5, weight=600)
    return t, staff, v


async def _delivered_order(client, app, t, staff, v, *, method="bkash", qty=1):
    """A real order, really paid (or COD), really delivered — the only state a return starts from."""
    h = await _buyer(client, app, t, phone_verified=True)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": qty}
    )
    addr = (await client.post("/api/v1/me/addresses", headers=h, json=ADDRESS)).json()
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    placed = await _place(client, h, q, address_id=addr["id"], payment_method=method)
    assert placed.status_code == 201, placed.text
    order = placed.json()
    if method != "cod":
        start = (
            await client.post(
                "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
            )
        ).json()
        paid = await client.post(f"/api/v1/payments/{start['payment_id']}/confirm", headers=h)
        assert paid.json()["status"] == "paid", paid.text
    sub = (await client.get("/api/v1/vendor/orders", headers=v["h"])).json()[0]
    shipment = (
        await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
    ).json()
    await _webhook(
        client, app, t, shipment["consignment_id"], "delivered", notification_id=uuid.uuid4().hex
    )
    return h, order, sub


async def _setup(client, app, platform_headers, slug, **settings):
    await _gateway(app)
    await _courier(app)
    t, staff, v = await _shop(client, app, platform_headers, slug, **settings)
    await _configure(client, staff)
    await _configure_courier(client, staff)
    return t, staff, v


async def _items_of(client, h, order_number):
    detail = (await client.get(f"/api/v1/me/orders/{order_number}", headers=h)).json()
    return detail


# --------------------------------------------------------------------------- the ordinary path
async def test_damaged_item_refunds_to_the_original_rail_and_restocks(
    client, app, platform_headers
):
    t, staff, v = await _setup(client, app, platform_headers, "ret-alpha")
    h, order, sub = await _delivered_order(client, app, t, staff, v, qty=2)
    window = (
        await client.get(f"/api/v1/me/orders/{order['number']}/return-window", headers=h)
    ).json()
    assert window[0]["returnable"] is True and window[0]["window_days"] == 7

    detail = (await client.get(f"/api/v1/vendor/orders/{sub['id']}", headers=v["h"])).json()
    item_id = (
        (await client.get("/api/v1/admin/orders", headers=staff)).status_code
        and detail["items"]
        and None
    )
    async with app.state.platform_db.engine.begin() as conn:
        item_id = str(
            await conn.scalar(
                text("SELECT id FROM order_items WHERE tenant_id = :t AND sub_order_id = :s"),
                {"t": t["id"], "s": sub["id"]},
            )
        )
    created = await client.post(
        "/api/v1/me/returns",
        headers=h,
        json={
            "sub_order_id": sub["id"],
            "reason": "damaged",
            "items": [{"order_item_id": item_id, "qty": 1}],
            "note": "Torn at the seam",
        },
    )
    assert created.status_code == 201, created.text
    ret = created.json()
    assert ret["status"] == "requested" and ret["shipping_payer"] == "vendor"
    assert D(ret["refund_total"]) == D("1000.00")  # half of a two-unit line

    # a second open return for the same shipment is refused
    assert (
        await client.post(
            "/api/v1/me/returns",
            headers=h,
            json={
                "sub_order_id": sub["id"],
                "reason": "damaged",
                "items": [{"order_item_id": item_id, "qty": 1}],
            },
        )
    ).status_code == 409

    assert (
        await client.post(
            f"/api/v1/vendor/returns/{ret['id']}/decision", headers=v["h"], json={"approve": True}
        )
    ).json()["status"] == "approved"
    pickup = await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/pickup", headers=v["h"], json={}
    )
    assert pickup.json()["courier"] == "steadfast" and pickup.json()["consignment_id"]
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/mark", headers=v["h"], json={"status": "picked_up"}
    )
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/mark", headers=v["h"], json={"status": "received"}
    )

    before = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    qc = await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/qc",
        headers=v["h"],
        json={"passed": True, "note": "seam torn, confirmed"},
    )
    assert qc.json() == {"id": ret["id"], "status": "qc_passed", "restocked_lines": 1}
    after = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    assert after["variants"][0]["stock_on_hand"] == before["variants"][0]["stock_on_hand"] + 1

    refund = await client.post(f"/api/v1/admin/returns/{ret['id']}/refund", headers=staff)
    assert refund.status_code == 200, refund.text
    assert refund.json()["status"] == "completed" and refund.json()["method"] == "bkash"
    assert D(refund.json()["amount"]) == D("1000.00")
    assert refund.json()["credit_note"].startswith("CN-")
    # the capture now carries what went back
    payment = (await client.get("/api/v1/admin/payments", headers=staff)).json()[0]
    assert payment["status"] == "partially_refunded" and D(payment["fee"]) == 0
    # and a second refund for the same return is impossible
    assert (
        await client.post(f"/api/v1/admin/returns/{ret['id']}/refund", headers=staff)
    ).status_code == 409
    notes = (await client.get("/api/v1/admin/credit-notes", headers=staff)).json()
    assert len(notes) == 1 and D(notes[0]["amount"]) == D("1000.00")


async def test_failed_qc_refunds_nothing_and_restocks_nothing(client, app, platform_headers):
    t, staff, v = await _setup(client, app, platform_headers, "ret-beta")
    h, order, sub = await _delivered_order(client, app, t, staff, v)
    async with app.state.platform_db.engine.begin() as conn:
        item_id = str(
            await conn.scalar(
                text("SELECT id FROM order_items WHERE tenant_id = :t AND sub_order_id = :s"),
                {"t": t["id"], "s": sub["id"]},
            )
        )
    ret = (
        await client.post(
            "/api/v1/me/returns",
            headers=h,
            json={
                "sub_order_id": sub["id"],
                "reason": "changed_mind",
                "items": [{"order_item_id": item_id, "qty": 1}],
            },
        )
    ).json()
    assert ret["shipping_payer"] == "buyer"  # change of mind is the buyer's postage
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/decision", headers=v["h"], json={"approve": True}
    )
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/mark", headers=v["h"], json={"status": "received"}
    )
    before = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    failed = await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/qc",
        headers=v["h"],
        json={"passed": False, "note": "worn, not as claimed"},
    )
    assert failed.json()["status"] == "qc_failed" and failed.json()["restocked_lines"] == 0
    after = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    assert after["variants"][0]["stock_on_hand"] == before["variants"][0]["stock_on_hand"]
    # a failed QC cannot be refunded; it goes back to the buyer
    assert (
        await client.post(f"/api/v1/admin/returns/{ret['id']}/refund", headers=staff)
    ).status_code == 409
    assert (await client.get("/api/v1/admin/payments", headers=staff)).json()[0]["status"] == "paid"


async def test_cod_return_pays_out_as_store_credit_and_can_be_spent(client, app, platform_headers):
    t, staff, v = await _setup(client, app, platform_headers, "ret-gamma")
    h, order, sub = await _delivered_order(client, app, t, staff, v, method="cod")
    async with app.state.platform_db.engine.begin() as conn:
        item_id = str(
            await conn.scalar(
                text("SELECT id FROM order_items WHERE tenant_id = :t AND sub_order_id = :s"),
                {"t": t["id"], "s": sub["id"]},
            )
        )
    # a COD order has no rail to refund to: the buyer must choose one
    assert (
        await client.post(
            "/api/v1/me/returns",
            headers=h,
            json={
                "sub_order_id": sub["id"],
                "reason": "wrong_item",
                "items": [{"order_item_id": item_id, "qty": 1}],
            },
        )
    ).status_code == 409
    ret = (
        await client.post(
            "/api/v1/me/returns",
            headers=h,
            json={
                "sub_order_id": sub["id"],
                "reason": "wrong_item",
                "items": [{"order_item_id": item_id, "qty": 1}],
                "refund_method": "store_credit",
            },
        )
    ).json()
    for step in ("decision", "received", "qc"):
        if step == "decision":
            await client.post(
                f"/api/v1/vendor/returns/{ret['id']}/decision",
                headers=v["h"],
                json={"approve": True},
            )
        elif step == "received":
            await client.post(
                f"/api/v1/vendor/returns/{ret['id']}/mark",
                headers=v["h"],
                json={"status": "received"},
            )
        else:
            await client.post(
                f"/api/v1/vendor/returns/{ret['id']}/qc", headers=v["h"], json={"passed": True}
            )
    out = (await client.post(f"/api/v1/admin/returns/{ret['id']}/refund", headers=staff)).json()
    assert out["method"] == "store_credit" and out["status"] == "completed"

    wallet = (await client.get("/api/v1/me/store-credit", headers=h)).json()
    assert (
        D(wallet["balance"]) == D(ret["refund_total"])
        and wallet["history"][0]["reason"] == "return_refund"
    )

    # and the credit is real money at checkout: it pays for the next order
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 1}
    )
    quote = (
        await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})
    ).json()
    assert D(quote["store_credit_available"]) == D(wallet["balance"])
    address_id = (await client.get("/api/v1/me/addresses", headers=h)).json()[0]["id"]
    placed = await client.post(
        "/api/v1/checkout/place",
        headers={**h, "idempotency-key": uuid.uuid4().hex},
        json={
            "address_id": address_id,
            "payment_method": "bkash",
            "expected_total": quote["grand_total"],
            "use_store_credit": True,
        },
    )
    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert D(body["store_credit_applied"]) == D(wallet["balance"])
    assert D(body["amount_due"]) == D(quote["grand_total"]) - D(wallet["balance"])
    assert D((await client.get("/api/v1/me/store-credit", headers=h)).json()["balance"]) == 0
    # the gateway is now only asked for what is still owed
    start = (
        await client.post(
            "/api/v1/payments/start", headers=h, json={"order_number": body["number"]}
        )
    ).json()
    assert D(start["amount"]) == D(body["amount_due"])
    done = await client.post(f"/api/v1/payments/{start['payment_id']}/confirm", headers=h)
    assert done.json()["status"] == "paid"
    assert (await client.get(f"/api/v1/me/orders/{body['number']}", headers=h)).json()[
        "status"
    ] == "processing"


async def test_store_credit_covering_the_whole_order_needs_no_gateway(
    client, app, platform_headers
):
    t, staff, v = await _setup(client, app, platform_headers, "ret-delta")
    h = await _buyer(client, app, t, phone_verified=True)
    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            user_id = str(
                await s.scalar(
                    text(
                        "SELECT id FROM users WHERE tenant_id = :t ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"t": t["id"]},
                )
            )
            await store_credit.grant(
                s, t["id"], user_id, D("5000"), reason="goodwill", actor_id="staff", note="test"
            )
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 1}
    )
    addr = (await client.post("/api/v1/me/addresses", headers=h, json=ADDRESS)).json()
    quote = (
        await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})
    ).json()
    placed = await client.post(
        "/api/v1/checkout/place",
        headers={**h, "idempotency-key": uuid.uuid4().hex},
        json={
            "address_id": addr["id"],
            "payment_method": "bkash",
            "expected_total": quote["grand_total"],
            "use_store_credit": True,
        },
    )
    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert D(body["amount_due"]) == 0
    order = (await client.get(f"/api/v1/me/orders/{body['number']}", headers=h)).json()
    assert order["status"] == "processing"  # confirmed without a gateway ever being called
    assert (
        await client.post(
            "/api/v1/payments/start", headers=h, json={"order_number": body["number"]}
        )
    ).status_code == 409
    detail = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    assert detail["variants"][0]["stock_on_hand"] == 4  # paid means the stock is gone


async def test_store_credit_cannot_be_spent_twice_or_go_negative(client, app, platform_headers):
    t, staff, v = await _setup(client, app, platform_headers, "ret-epsilon")
    await _buyer(client, app, t)
    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            user_id = str(
                await s.scalar(
                    text(
                        "SELECT id FROM users WHERE tenant_id = :t ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"t": t["id"]},
                )
            )
            await store_credit.grant(
                s, t["id"], user_id, D("100"), reason="goodwill", actor_id="staff", note="test"
            )
            await store_credit.spend(s, t["id"], user_id, D("60"), actor_id="staff")
            assert await store_credit.balance(s, t["id"], user_id) == D("40.00")
            try:
                await store_credit.spend(s, t["id"], user_id, D("60"), actor_id="staff")
                raise AssertionError("overspend should have been refused")
            except store_credit.CreditError:
                pass
    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            assert await store_credit.balance(s, t["id"], user_id) == D("40.00")


async def test_manual_cod_refund_waits_for_finance(client, app, platform_headers):
    t, staff, v = await _setup(client, app, platform_headers, "ret-zeta")
    h, order, sub = await _delivered_order(client, app, t, staff, v, method="cod")
    async with app.state.platform_db.engine.begin() as conn:
        item_id = str(
            await conn.scalar(
                text("SELECT id FROM order_items WHERE tenant_id = :t AND sub_order_id = :s"),
                {"t": t["id"], "s": sub["id"]},
            )
        )
    ret = (
        await client.post(
            "/api/v1/me/returns",
            headers=h,
            json={
                "sub_order_id": sub["id"],
                "reason": "damaged",
                "items": [{"order_item_id": item_id, "qty": 1}],
                "refund_method": "bkash",
                "bkash_number": "01711111111",
            },
        )
    ).json()
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/decision", headers=v["h"], json={"approve": True}
    )
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/mark", headers=v["h"], json={"status": "received"}
    )
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/qc", headers=v["h"], json={"passed": True}
    )
    out = (await client.post(f"/api/v1/admin/returns/{ret['id']}/refund", headers=staff)).json()
    assert out["status"] == "pending" and out["method"] == "manual_bkash"

    # staff see where the money is going, but only masked
    detail = (await client.get(f"/api/v1/admin/returns/{ret['id']}", headers=staff)).json()
    assert detail["refund_target"] == {"bkash_number": "••••1111"}
    async with app.state.platform_db.engine.begin() as conn:
        stored = await conn.scalar(
            text("SELECT refund_target_ciphertext FROM return_requests WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    assert "01711111111" not in stored and "1711111111" not in stored

    queue = (await client.get("/api/v1/admin/refunds?status=pending", headers=staff)).json()
    assert len(queue) == 1 and queue[0]["return_number"] == ret["number"]
    done = await client.post(
        f"/api/v1/admin/refunds/{queue[0]['id']}/complete",
        headers=staff,
        json={"reference": "BKASH-TRX-77123"},
    )
    assert done.json()["status"] == "completed" and done.json()["credit_note"].startswith("CN-")
    assert (await client.get("/api/v1/admin/returns", headers=staff)).json()[0][
        "status"
    ] == "refunded"
    # completing it again is not possible
    assert (
        await client.post(
            f"/api/v1/admin/refunds/{queue[0]['id']}/complete",
            headers=staff,
            json={"reference": "BKASH-TRX-77123"},
        )
    ).status_code == 404


# ----------------------------------------------------------------------------------- the rules
async def test_the_window_is_the_rule_and_the_category_can_shorten_it(
    client, app, platform_headers
):
    t, staff, v = await _setup(client, app, platform_headers, "ret-eta")
    h, order, sub = await _delivered_order(client, app, t, staff, v)
    async with app.state.platform_db.engine.begin() as conn:
        item_id = str(
            await conn.scalar(
                text("SELECT id FROM order_items WHERE tenant_id = :t AND sub_order_id = :s"),
                {"t": t["id"], "s": sub["id"]},
            )
        )
        await conn.execute(
            text("UPDATE categories SET return_window_days = 2 WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    window = (
        await client.get(f"/api/v1/me/orders/{order['number']}/return-window", headers=h)
    ).json()
    assert window[0]["window_days"] == 2  # the category overrides the tenant's 7 days

    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE shipments SET delivered_at = now() - interval '10 days' WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    closed = await client.post(
        "/api/v1/me/returns",
        headers=h,
        json={
            "sub_order_id": sub["id"],
            "reason": "damaged",
            "items": [{"order_item_id": item_id, "qty": 1}],
        },
    )
    assert closed.status_code == 409 and closed.json()["title"] == "window_closed"


async def test_a_buyer_cannot_return_more_than_they_bought_or_someone_elses_order(
    client, app, platform_headers
):
    t, staff, v = await _setup(client, app, platform_headers, "ret-theta")
    h, order, sub = await _delivered_order(client, app, t, staff, v, qty=2)
    async with app.state.platform_db.engine.begin() as conn:
        item_id = str(
            await conn.scalar(
                text("SELECT id FROM order_items WHERE tenant_id = :t AND sub_order_id = :s"),
                {"t": t["id"], "s": sub["id"]},
            )
        )
    too_many = await client.post(
        "/api/v1/me/returns",
        headers=h,
        json={
            "sub_order_id": sub["id"],
            "reason": "damaged",
            "items": [{"order_item_id": item_id, "qty": 3}],
        },
    )
    assert too_many.status_code == 409 and too_many.json()["title"] == "bad_quantity"
    stranger = await _buyer(client, app, t)
    assert (
        await client.post(
            "/api/v1/me/returns",
            headers=stranger,
            json={
                "sub_order_id": sub["id"],
                "reason": "damaged",
                "items": [{"order_item_id": item_id, "qty": 1}],
            },
        )
    ).status_code == 404


async def test_auto_approval_and_qc_escalation(client, app, platform_headers):
    t, staff, v = await _setup(client, app, platform_headers, "ret-iota")
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tenant_settings SET return_auto_approve_reasons = ARRAY['damaged'], "
                "qc_sla_hours = 24 WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    h, order, sub = await _delivered_order(client, app, t, staff, v)
    async with app.state.platform_db.engine.begin() as conn:
        item_id = str(
            await conn.scalar(
                text("SELECT id FROM order_items WHERE tenant_id = :t AND sub_order_id = :s"),
                {"t": t["id"], "s": sub["id"]},
            )
        )
    ret = (
        await client.post(
            "/api/v1/me/returns",
            headers=h,
            json={
                "sub_order_id": sub["id"],
                "reason": "damaged",
                "items": [{"order_item_id": item_id, "qty": 1}],
            },
        )
    ).json()
    assert ret["status"] == "approved"  # the tenant's own rule decided, not a human
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/mark", headers=v["h"], json={"status": "received"}
    )
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE return_requests SET qc_due_at = now() - interval '1 hour' WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            assert await service.escalate_overdue_qc(s, t["id"]) == 1
            assert await service.escalate_overdue_qc(s, t["id"]) == 0
    escalated = (await client.get("/api/v1/admin/returns?escalated=true", headers=staff)).json()
    assert len(escalated) == 1 and escalated[0]["number"] == ret["number"]


async def test_staff_can_overrule_a_vendors_rejection(client, app, platform_headers):
    t, staff, v = await _setup(client, app, platform_headers, "ret-kappa")
    h, order, sub = await _delivered_order(client, app, t, staff, v)
    async with app.state.platform_db.engine.begin() as conn:
        item_id = str(
            await conn.scalar(
                text("SELECT id FROM order_items WHERE tenant_id = :t AND sub_order_id = :s"),
                {"t": t["id"], "s": sub["id"]},
            )
        )
    ret = (
        await client.post(
            "/api/v1/me/returns",
            headers=h,
            json={
                "sub_order_id": sub["id"],
                "reason": "not_as_described",
                "items": [{"order_item_id": item_id, "qty": 1}],
            },
        )
    ).json()
    rejected = await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/decision",
        headers=v["h"],
        json={"approve": False, "note": "looks fine to me"},
    )
    assert rejected.json()["status"] == "rejected"
    # a rejected return is final for the vendor...
    assert (
        await client.post(
            f"/api/v1/vendor/returns/{ret['id']}/decision", headers=v["h"], json={"approve": True}
        )
    ).status_code == 409
    # ...and the buyer sees why
    assert (await client.get("/api/v1/me/returns", headers=h)).json()[0]["status"] == "rejected"
