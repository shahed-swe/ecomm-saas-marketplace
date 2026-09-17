"""Phase 15: trust.

A marketplace runs on the belief that reviews are real, that a dispute stops the money, that the
vendor scorecard measures behaviour rather than opinion, and that nobody can quietly pull a deal
off the platform in a chat window. Each of those beliefs gets a test here.
"""

import uuid
from decimal import Decimal as D

from sqlalchemy import text

from app.modules.trust import filters
from app.modules.trust import service as trust
from app.tests.test_checkout import ADDRESS, _buyer, _market, _place, _vendor
from app.tests.test_fulfilment import _configure_courier, _courier, _webhook
from app.tests.test_payments import _configure, _gateway


async def _shop(client, app, platform_headers, slug, **settings):
    await _gateway(app)
    await _courier(app)
    t, staff, cat = await _market(client, app, platform_headers, **settings)
    v = await _vendor(client, app, t, staff, slug, cat, price="1000", stock=10, weight=600)
    await _configure(client, staff)
    await _configure_courier(client, staff)
    return t, staff, v


async def _delivered(client, app, t, v, *, deliver=True, qty=1):
    h = await _buyer(client, app, t, phone_verified=True)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": qty}
    )
    addr = (await client.post("/api/v1/me/addresses", headers=h, json=ADDRESS)).json()
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    order = (await _place(client, h, q, address_id=addr["id"], payment_method="bkash")).json()
    start = (
        await client.post(
            "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
        )
    ).json()
    await client.post(f"/api/v1/payments/{start['payment_id']}/confirm", headers=h)
    sub = (await client.get("/api/v1/vendor/orders", headers=v["h"])).json()[0]
    if deliver:
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
    detail = (await client.get(f"/api/v1/me/orders/{order['number']}", headers=h)).json()
    return h, order, sub, detail["items"][0]["id"]


# -------------------------------------------------------------------------------------- reviews
async def test_only_a_delivered_purchase_can_be_reviewed(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "trust-alpha")
    h, order, sub, item_id = await _delivered(client, app, t, v, deliver=False)
    early = await client.post(
        "/api/v1/me/reviews", headers=h, json={"order_item_id": item_id, "rating": 5}
    )
    assert early.status_code == 409 and early.json()["title"] == "not_delivered"

    h2, order2, sub2, item2 = await _delivered(client, app, t, v)
    written = await client.post(
        "/api/v1/me/reviews",
        headers=h2,
        json={
            "order_item_id": item2,
            "rating": 4,
            "title": "Good",
            "body": "Soft cotton, true size",
        },
    )
    assert written.status_code == 201 and written.json()["status"] == "published"
    # one review per purchased line, ever
    assert (
        await client.post(
            "/api/v1/me/reviews", headers=h2, json={"order_item_id": item2, "rating": 1}
        )
    ).status_code == 409
    # and a stranger cannot review a line they did not buy
    assert (
        await client.post(
            "/api/v1/me/reviews", headers=h, json={"order_item_id": item2, "rating": 5}
        )
    ).status_code == 404

    public = (await client.get(f"/api/v1/products/{v['product_id']}/reviews", headers=h)).json()
    assert D(public["rating_avg"]) == D("4.00") and public["rating_count"] == 1
    assert public["reviews"][0]["title"] == "Good"
    assert "@" not in public["reviews"][0]["author"]  # never an email address
    assert public["histogram"] == {"4": 1}

    reply = await client.post(
        f"/api/v1/vendor/reviews/{written.json()['id']}/reply",
        headers=v["h"],
        json={"reply": "Thank you! Message us on 01712345678 for offers"},
    )
    assert reply.status_code == 200
    assert "[hidden]" in reply.json()["reply"] and "phone" in reply.json()["flags"]
    # a vendor answers once
    assert (
        await client.post(
            f"/api/v1/vendor/reviews/{written.json()['id']}/reply",
            headers=v["h"],
            json={"reply": "again"},
        )
    ).status_code == 409


async def test_a_flagged_review_waits_for_a_human(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "trust-beta")
    h, order, sub, item_id = await _delivered(client, app, t, v)
    written = (
        await client.post(
            "/api/v1/me/reviews",
            headers=h,
            json={
                "order_item_id": item_id,
                "rating": 1,
                "body": "Call me at 01812345678, I sell the same thing cheaper",
            },
        )
    ).json()
    assert written["status"] == "pending" and "phone" in written["flags"]
    # nothing pending moves a star
    product = (await client.get(f"/api/v1/products/{v['product_id']}/reviews", headers=h)).json()
    assert product["rating_count"] == 0 and product["reviews"] == []
    queue = (await client.get("/api/v1/admin/reviews?status=pending", headers=staff)).json()
    assert len(queue) == 1 and "phone" in queue[0]["flagged_reasons"]
    assert (
        await client.post(
            f"/api/v1/admin/reviews/{written['id']}/moderate",
            headers=staff,
            json={"decision": "reject"},
        )
    ).json()["status"] == "rejected"
    assert (await client.get(f"/api/v1/products/{v['product_id']}/reviews", headers=h)).json()[
        "rating_count"
    ] == 0


async def test_helpful_votes_count_once_per_buyer(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "trust-gamma")
    h, order, sub, item_id = await _delivered(client, app, t, v)
    review = (
        await client.post(
            "/api/v1/me/reviews", headers=h, json={"order_item_id": item_id, "rating": 5}
        )
    ).json()
    other = await _buyer(client, app, t)
    for _ in range(3):
        assert (
            await client.post(f"/api/v1/reviews/{review['id']}/helpful", headers=other)
        ).status_code == 204
    public = (await client.get(f"/api/v1/products/{v['product_id']}/reviews", headers=h)).json()
    assert public["reviews"][0]["helpful_count"] == 1


# ------------------------------------------------------------------------------------- disputes
async def test_a_dispute_holds_the_money_until_it_is_resolved(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "trust-delta")
    async with app.state.platform_db.engine.begin() as conn:
        # the return-window reserve is Phase 14's job; here the hold is the only thing in play
        await conn.execute(
            text("UPDATE tenant_settings SET reserve_mode = 'none' WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    h, order, sub, item_id = await _delivered(client, app, t, v)
    balance = (await client.get("/api/v1/vendor/balance", headers=v["h"])).json()
    assert D(balance["payable"]) > 0 and balance["on_hold"] is False

    opened = await client.post(
        "/api/v1/me/disputes",
        headers=h,
        json={
            "sub_order_id": sub["id"],
            "reason": "not_as_described",
            "detail": "Different colour",
        },
    )
    assert opened.status_code == 201, opened.text
    dispute = opened.json()
    assert dispute["number"].startswith("DSP-") and D(dispute["amount"]) == D(sub["total"])
    held = (await client.get("/api/v1/vendor/balance", headers=v["h"])).json()
    assert held["on_hold"] is True and D(held["available"]) == 0

    # one open dispute per shipment
    assert (
        await client.post(
            "/api/v1/me/disputes",
            headers=h,
            json={"sub_order_id": sub["id"], "reason": "damaged"},
        )
    ).status_code == 409

    # both sides can speak, and the state follows whose turn it is
    assert (
        await client.post(
            f"/api/v1/vendor/disputes/{dispute['id']}/messages",
            headers=v["h"],
            json={"body": "We sent the right colour, here is the packing photo"},
        )
    ).json()["status"] == "under_review"
    thread = (await client.get(f"/api/v1/admin/disputes/{dispute['id']}", headers=staff)).json()
    assert [m["author_kind"] for m in thread["messages"]] == ["vendor"]

    resolved = await client.post(
        f"/api/v1/admin/disputes/{dispute['id']}/resolve",
        headers=staff,
        json={"in_favour_of": "vendor", "note": "photos match the listing"},
    )
    assert resolved.json()["status"] == "resolved_vendor"
    # whoever wins, the vendor's liquidity comes back
    after = (await client.get("/api/v1/vendor/balance", headers=v["h"])).json()
    assert after["on_hold"] is False and D(after["available"]) > 0
    # and a closed dispute takes no more messages
    assert (
        await client.post(
            f"/api/v1/vendor/disputes/{dispute['id']}/messages",
            headers=v["h"],
            json={"body": "one more thing"},
        )
    ).status_code == 409


async def test_an_ignored_dispute_escalates_by_itself(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "trust-epsilon")
    h, order, sub, item_id = await _delivered(client, app, t, v)
    await client.post(
        "/api/v1/me/disputes", headers=h, json={"sub_order_id": sub["id"], "reason": "not_received"}
    )
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE disputes SET due_at = now() - interval '1 hour' WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        assert await trust.escalate_overdue_disputes(s, t["id"]) == 1
        assert await trust.escalate_overdue_disputes(s, t["id"]) == 0
    assert (await client.get("/api/v1/admin/disputes?status=under_review", headers=staff)).json()[
        0
    ]["reason"] == "not_received"


# ------------------------------------------------------------------------------------ messaging
async def test_chat_redacts_contact_details_and_records_the_attempt(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "trust-zeta")
    h, order, sub, item_id = await _delivered(client, app, t, v)
    sent = await client.post(
        "/api/v1/me/messages",
        headers=h,
        json={"vendor_id": v["id"], "sub_order_id": sub["id"], "body": "Is the blue one in stock?"},
    )
    assert sent.status_code == 201 and sent.json()["redacted"] is False
    conversation_id = sent.json()["conversation_id"]

    leak = await client.post(
        f"/api/v1/vendor/messages/{conversation_id}",
        headers=v["h"],
        json={"body": "Yes! WhatsApp me on ০১৭১১১২২৩৩৪ and pay bKash, cheaper"},
    )
    assert leak.status_code == 201
    assert "[hidden]" in leak.json()["body"] and "০১৭১১" not in leak.json()["body"]
    assert set(leak.json()["flags"]) >= {"phone", "social_handle", "off_platform_payment"}

    thread = (await client.get(f"/api/v1/me/messages/{conversation_id}", headers=h)).json()
    assert len(thread) == 2 and thread[1]["redacted"] is True
    flagged = (await client.get("/api/v1/admin/messages/flagged", headers=staff)).json()
    assert len(flagged) == 1 and flagged[0]["vendor_name"]

    # the buyer's thread list shows the unread and staff can end the conversation
    threads = (await client.get("/api/v1/me/messages", headers=h)).json()
    assert threads[0]["id"] == conversation_id
    assert (
        await client.post(f"/api/v1/admin/conversations/{conversation_id}/block", headers=staff)
    ).status_code == 204
    assert (
        await client.post(
            f"/api/v1/vendor/messages/{conversation_id}", headers=v["h"], json={"body": "hello?"}
        )
    ).status_code == 409


async def test_messages_cannot_be_edited_after_the_fact(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "trust-eta")
    h, order, sub, item_id = await _delivered(client, app, t, v)
    sent = (
        await client.post(
            "/api/v1/me/messages",
            headers=h,
            json={"vendor_id": v["id"], "body": "When will it ship?"},
        )
    ).json()
    import sqlalchemy.exc

    try:
        async with app.state.platform_db.engine.begin() as conn:
            await conn.execute(
                text("UPDATE messages SET body = 'rewritten' WHERE tenant_id = :t"),
                {"t": t["id"]},
            )
        raise AssertionError("messages must be append-only")
    except sqlalchemy.exc.DatabaseError:
        pass
    thread = (await client.get(f"/api/v1/me/messages/{sent['conversation_id']}", headers=h)).json()
    assert thread[0]["body"] == "When will it ship?"


def test_the_filter_understands_how_people_actually_write():
    assert filters.scan("order 3 pieces at 4500 taka") == []
    assert "phone" in filters.scan("amar number 017-11-223344")
    assert "phone" in filters.scan("০১৭১১২২৩৩৪৪ e call diyen")
    assert "email" in filters.scan("mail: shop.owner@gmail.com")
    assert "link" in filters.scan("dekhen amarshop.com.bd")
    assert filters.redact("normal talk")[0] == "normal talk"
    # the original is never stored, only a fingerprint of it
    assert len(filters.fingerprint("01711223344")) == 64


# ------------------------------------------------------------------------------ vendor scoring
async def test_the_scorecard_measures_behaviour_not_opinion(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "trust-theta")
    for _ in range(3):
        await _delivered(client, app, t, v)
    scored = await client.post("/api/v1/admin/vendor-scores/recompute", headers=staff)
    assert scored.json()["scored"] >= 1
    scores = (await client.get("/api/v1/admin/vendor-scores", headers=staff)).json()
    mine = next(s for s in scores if s["vendor_name"] == "Trust-Theta")
    assert mine["orders"] == 3 and D(mine["on_time_rate"]) == 1 and mine["band"] == "good"

    # a disputed, cancelled, returned history drags the same vendor down
    h, order, sub, item_id = await _delivered(client, app, t, v)
    await client.post(
        "/api/v1/me/disputes", headers=h, json={"sub_order_id": sub["id"], "reason": "not_received"}
    )
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sub_orders SET status = 'returned' WHERE tenant_id = :t AND id IN "
                "(SELECT id FROM sub_orders WHERE tenant_id = :t ORDER BY created_at LIMIT 2)"
            ),
            {"t": t["id"]},
        )
    card = (await client.get("/api/v1/vendor/scorecard", headers=v["h"])).json()
    assert D(card["return_rate"]) > 0 and D(card["dispute_rate"]) > 0
    assert D(card["score"]) < D(str(mine["score"]))
    assert card["band"] in ("good", "watch", "risk")
