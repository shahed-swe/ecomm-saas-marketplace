"""Phase 16: telling people things.

The failure modes worth testing are the rude ones: messaging someone who opted out, waking a buyer
at 2am with a marketing push, sending the same "your order shipped" three times because a webhook
retried, and losing the purchase event the tenant's ad spend is measured on.
"""

import uuid

from sqlalchemy import text

from app.modules.notifications import analytics, campaigns
from app.modules.notifications import service as notifications
from app.modules.notifications.channels import FakeEmail, FakePush
from app.tests.test_checkout import ADDRESS, _buyer, _market, _place, _vendor
from app.tests.test_fulfilment import _configure_courier, _courier, _webhook
from app.tests.test_payments import _configure, _gateway


async def _channels(app):
    push, email = FakePush(), FakeEmail()
    app.state.push, app.state.email = push, email
    app.state.sms.sent.clear()
    return push, email


async def _shop(client, app, platform_headers, slug, *, quiet_hours=False):
    await _gateway(app)
    await _courier(app)
    t, staff, cat = await _market(client, app, platform_headers)
    v = await _vendor(client, app, t, staff, slug, cat, price="1000", stock=10, weight=600)
    await _configure(client, staff)
    await _configure_courier(client, staff)
    if not quiet_hours:
        # tests run at whatever hour CI feels like: equal start and end means "no quiet hours"
        async with app.state.platform_db.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tenant_settings SET marketing_quiet_start = 0, marketing_quiet_end = 0 "
                    "WHERE tenant_id = :t"
                ),
                {"t": t["id"]},
            )
    return t, staff, v


async def _shopper(client, app, t, *, device=True):
    h = await _buyer(client, app, t, phone_verified=True)
    if device:
        assert (
            await client.post(
                "/api/v1/me/devices",
                headers=h,
                json={"token": f"tok-{uuid.uuid4().hex}", "platform": "android"},
            )
        ).status_code == 201
    return h


async def _order(client, app, t, v, h, *, pay=True, ship=False):
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 1}
    )
    addr = (await client.post("/api/v1/me/addresses", headers=h, json=ADDRESS)).json()
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    order = (await _place(client, h, q, address_id=addr["id"], payment_method="bkash")).json()
    if pay:
        start = (
            await client.post(
                "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
            )
        ).json()
        await client.post(f"/api/v1/payments/{start['payment_id']}/confirm", headers=h)
    sub = (await client.get("/api/v1/vendor/orders", headers=v["h"])).json()[0]
    consignment = None
    if ship:
        booked = (
            await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
        ).json()
        consignment = booked["consignment_id"]
    return order, sub, consignment


# ---------------------------------------------------------------------------- the ordinary path
async def test_an_order_tells_the_buyer_on_every_channel_that_fits(client, app, platform_headers):
    push, email = await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-alpha")
    h = await _shopper(client, app, t)
    order, sub, consignment = await _order(client, app, t, v, h, ship=True)

    inbox = (await client.get("/api/v1/me/notifications", headers=h)).json()
    keys = [i["key"] for i in inbox["items"]]
    assert "order.placed" in keys and "payment.paid" in keys
    assert inbox["unread"] == len(inbox["items"])
    assert push.sent and any("order" in s["body"].lower() or "অর্ডার" in s["body"] for s in push.sent)
    assert app.state.sms.sent  # an SMS goes out too, because not every buyer installs the app

    await _webhook(client, app, t, consignment, "in_transit", notification_id=uuid.uuid4().hex)
    await _webhook(client, app, t, consignment, "delivered", notification_id=uuid.uuid4().hex)
    inbox = (await client.get("/api/v1/me/notifications", headers=h)).json()
    keys = [i["key"] for i in inbox["items"]]
    assert "shipment.shipped" in keys and "shipment.delivered" in keys

    assert (await client.post("/api/v1/me/notifications/read", headers=h)).status_code == 204
    assert (await client.get("/api/v1/me/notifications", headers=h)).json()["unread"] == 0


async def test_the_same_event_never_notifies_twice(client, app, platform_headers):
    push, _ = await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-beta")
    h = await _shopper(client, app, t)
    order, sub, consignment = await _order(client, app, t, v, h, ship=True)
    event_id = uuid.uuid4().hex
    await _webhook(client, app, t, consignment, "in_transit", notification_id=event_id)
    before = len(push.sent)
    # the courier retries the very same notification, and a new one for the same status
    await _webhook(client, app, t, consignment, "in_transit", notification_id=event_id)
    await _webhook(client, app, t, consignment, "in_transit", notification_id=uuid.uuid4().hex)
    assert len(push.sent) == before
    shipped = [
        i
        for i in (await client.get("/api/v1/me/notifications", headers=h)).json()["items"]
        if i["key"] == "shipment.shipped"
    ]
    assert len(shipped) == 1


async def test_a_tenant_can_reword_anything(client, app, platform_headers):
    push, _ = await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-gamma")
    assert (
        await client.put(
            "/api/v1/admin/notification-templates",
            headers=staff,
            json={
                "key": "order.placed",
                "channel": "push",
                "locale": "bn",
                "subject": "ধন্যবাদ!",
                "body": "{store}-এ অর্ডার {number} পেয়েছি।",
            },
        )
    ).status_code == 200
    h = await _shopper(client, app, t)
    order, sub, _ = await _order(client, app, t, v, h, pay=False)
    titles = [s["title"] for s in push.sent]
    assert "ধন্যবাদ!" in titles
    assert any(order["number"] in s["body"] for s in push.sent)

    # turning a template off silences that one channel and nothing else
    await client.put(
        "/api/v1/admin/notification-templates",
        headers=staff,
        json={
            "key": "order.placed",
            "channel": "sms",
            "locale": "bn",
            "body": "x",
            "enabled": False,
        },
    )
    app.state.sms.sent.clear()
    h2 = await _shopper(client, app, t)
    await _order(client, app, t, v, h2, pay=False)
    assert not [s for s in app.state.sms.sent if "order" in s[2].lower()]


# ------------------------------------------------------------------------------------ marketing
async def test_marketing_respects_opt_out_and_quiet_hours(client, app, platform_headers):
    push, _ = await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-delta")
    h = await _shopper(client, app, t)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 2}
    )
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE carts SET updated_at = now() - interval '2 days' WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        out = await campaigns.nudge_abandoned_carts(s, app.state, t["id"])
    assert out == {"carts": 1, "nudged": 1}
    assert any(
        "cart" in (x["title"] or "").lower() or "কার্ট" in (x["title"] or "") for x in push.sent
    )

    # the same cart is not nudged again the same day
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        assert (await campaigns.nudge_abandoned_carts(s, app.state, t["id"]))["nudged"] == 0

    # a buyer who opts out of marketing push is left alone — transactional still arrives
    await client.put(
        "/api/v1/me/notification-preferences",
        headers=h,
        json={"marketing_push": False, "marketing_sms": False, "marketing_email": False},
    )
    campaign = (
        await client.post(
            "/api/v1/admin/push-campaigns",
            headers=staff,
            json={
                "name": "Eid sale",
                "segment": "all",
                "title": "Eid offers",
                "body": "Up to 50% off",
            },
        )
    ).json()
    assert campaign["audience"] >= 1
    sent = (
        await client.post(f"/api/v1/admin/push-campaigns/{campaign['id']}/send", headers=staff)
    ).json()
    assert sent["sent"] == 0 and sent["suppressed"] >= 1
    # ...and the order confirmation for the same person still goes out
    before = len(push.sent)
    await _order(client, app, t, v, h, pay=False)
    assert len(push.sent) > before


async def test_a_campaign_sends_once_and_records_what_it_did(client, app, platform_headers):
    push, _ = await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-epsilon")
    for _ in range(3):
        await _shopper(client, app, t)
    campaign = (
        await client.post(
            "/api/v1/admin/push-campaigns",
            headers=staff,
            json={
                "name": "Winter",
                "segment": "all",
                "title": "Winter collection",
                "body": "New arrivals",
            },
        )
    ).json()
    assert campaign["audience"] == 3, campaign
    first = (
        await client.post(f"/api/v1/admin/push-campaigns/{campaign['id']}/send", headers=staff)
    ).json()
    assert first["sent"] == 3 and first["audience"] == 3, first
    second = (
        await client.post(f"/api/v1/admin/push-campaigns/{campaign['id']}/send", headers=staff)
    ).json()
    assert second.get("already") is True
    listed = (await client.get("/api/v1/admin/push-campaigns", headers=staff)).json()
    assert listed[0]["sent_count"] == 3 and listed[0]["status"] == "sent"


async def test_a_revoked_device_stops_receiving(client, app, platform_headers):
    push, _ = await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-zeta")
    h = await _buyer(client, app, t, phone_verified=True)
    token = f"tok-{uuid.uuid4().hex}"
    await client.post("/api/v1/me/devices", headers=h, json={"token": token, "platform": "ios"})
    assert (await client.delete(f"/api/v1/me/devices/{token}", headers=h)).status_code == 204
    before = len(push.sent)
    await _order(client, app, t, v, h, pay=False)
    assert len(push.sent) == before  # nothing to push to
    log = (await client.get("/api/v1/admin/notifications?status=suppressed", headers=staff)).json()
    assert any(x["key"] == "order.placed" for x in log)
    # the in-app copy is still there when they next open the app
    assert [
        i["key"] for i in (await client.get("/api/v1/me/notifications", headers=h)).json()["items"]
    ] == ["order.placed"]


# ------------------------------------------------------------------------------------ analytics
async def test_the_purchase_event_is_queued_server_side_and_deduplicated(
    client, app, platform_headers
):
    await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-eta")
    saved = await client.put(
        "/api/v1/admin/analytics-destinations",
        headers=staff,
        json={"provider": "ga4", "public_id": "G-ABC123", "secret": "super-secret-key"},
    )
    assert saved.status_code == 200
    view = saved.json()[0]
    assert view["secret_set"] is True and "super-secret-key" not in str(view)
    public = (
        await client.get("/api/v1/storefront/analytics", headers={"host": t["primary_host"]})
    ).json()
    assert public == {"ga4": "G-ABC123"}  # the page gets the id, never the secret

    h = await _shopper(client, app, t)
    order, sub, _ = await _order(client, app, t, v, h)
    async with app.state.platform_db.engine.begin() as conn:
        events = (
            (
                await conn.execute(
                    text(
                        "SELECT provider, name, status, payload FROM analytics_events WHERE tenant_id = :t"
                    ),
                    {"t": t["id"]},
                )
            )
            .mappings()
            .all()
        )
    assert len(events) == 1 and events[0]["name"] == "purchase" and events[0]["status"] == "queued"
    assert events[0]["payload"]["number"] == order["number"]
    assert "@" not in str(events[0]["payload"].get("email_hash") or "")  # hashed, never raw

    # flushing sends it once; a second flush has nothing left to do
    class FakeHttp:
        def __init__(self):
            self.posts = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            self.posts.append((url, kwargs))

            class R:
                status_code = 204

            return R()

    http = FakeHttp()
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        out = await analytics.flush(s, app.state.settings, t["id"], client=http)
        assert out["sent"] == 1
    assert "google-analytics.com" in http.posts[0][0]
    assert http.posts[0][1]["params"]["api_secret"] == "super-secret-key"
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        assert (await analytics.flush(s, app.state.settings, t["id"], client=FakeHttp()))[
            "sent"
        ] == 0


def test_identifiers_are_hashed_the_way_the_providers_ask():
    assert analytics.hashed("  Buyer@Example.COM ") == analytics.hashed("buyer@example.com")
    assert len(analytics.hashed("01711223344")) == 64
    assert analytics.hashed(None) is None


# ------------------------------------------------------------------------------------- support
async def test_a_support_ticket_runs_its_course(client, app, platform_headers):
    await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-theta")
    h = await _shopper(client, app, t)
    order, sub, _ = await _order(client, app, t, v, h)
    opened = await client.post(
        "/api/v1/me/tickets",
        headers=h,
        json={
            "subject": "Wrong size delivered",
            "body": "I ordered L but got M. Call me on 01712345678",
            "category": "order",
            "order_number": order["number"],
        },
    )
    assert opened.status_code == 201, opened.text
    ticket = opened.json()
    assert ticket["number"].startswith("TKT-")

    mine = (await client.get(f"/api/v1/me/tickets/{ticket['id']}", headers=h)).json()
    assert "[hidden]" in mine["messages"][0]["body"]  # the same filter as chat

    queue = (await client.get("/api/v1/admin/tickets", headers=staff)).json()
    assert queue[0]["number"] == ticket["number"] and queue[0]["status"] == "open"
    await client.post(
        f"/api/v1/admin/tickets/{ticket['id']}/messages",
        headers=staff,
        json={"body": "Checking with the seller now", "internal": True},
    )
    await client.post(
        f"/api/v1/admin/tickets/{ticket['id']}/messages",
        headers=staff,
        json={"body": "Sorry about that — we are sending the right size today."},
    )
    # the customer sees the reply but never the internal note
    customer_view = (await client.get(f"/api/v1/me/tickets/{ticket['id']}", headers=h)).json()
    bodies = [m["body"] for m in customer_view["messages"]]
    assert "Checking with the seller now" not in bodies
    assert any("right size today" in b for b in bodies)
    assert customer_view["status"] == "pending_customer"
    staff_view = (await client.get(f"/api/v1/admin/tickets/{ticket['id']}", headers=staff)).json()
    assert any(m["internal"] for m in staff_view["messages"])
    assert staff_view["first_response_at"] is not None

    assert (
        await client.post(
            f"/api/v1/me/tickets/{ticket['id']}/messages", headers=h, json={"body": "Thank you!"}
        )
    ).json()["status"] == "pending_staff"
    closed = await client.post(
        f"/api/v1/admin/tickets/{ticket['id']}/status", headers=staff, json={"status": "closed"}
    )
    assert closed.json()["status"] == "closed"
    assert (
        await client.post(
            f"/api/v1/me/tickets/{ticket['id']}/messages", headers=h, json={"body": "one more"}
        )
    ).status_code == 409

    # someone else's ticket does not exist
    stranger = await _buyer(client, app, t)
    assert (
        await client.get(f"/api/v1/me/tickets/{ticket['id']}", headers=stranger)
    ).status_code == 404


async def test_a_vendor_can_raise_a_ticket_too(client, app, platform_headers):
    await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-iota")
    opened = await client.post(
        "/api/v1/vendor/tickets",
        headers=v["h"],
        json={
            "subject": "Payout missing",
            "body": "Last week's payout has not arrived",
            "category": "payment",
        },
    )
    assert opened.status_code == 201
    queue = (await client.get("/api/v1/admin/tickets", headers=staff)).json()
    assert queue[0]["vendor_name"] == "Note-Iota" and queue[0]["customer_email"] is None


def test_quiet_hours_wrap_around_midnight():
    from datetime import UTC, datetime

    # 22:00–08:00 local (UTC+6) means 16:00–02:00 UTC
    at_2300_local = datetime(2026, 9, 17, 17, 0, tzinfo=UTC)
    at_1500_local = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    assert notifications._in_quiet_hours(at_2300_local, 22, 8) is True
    assert notifications._in_quiet_hours(at_1500_local, 22, 8) is False
    assert notifications._in_quiet_hours(at_1500_local, None, None) is False


async def test_a_flushed_event_that_the_provider_rejects_stays_visible(
    client, app, platform_headers
):
    await _channels(app)
    t, staff, v = await _shop(client, app, platform_headers, "note-kappa")
    await client.put(
        "/api/v1/admin/analytics-destinations",
        headers=staff,
        json={"provider": "meta", "public_id": "1234567890", "secret": "capi-token"},
    )
    h = await _shopper(client, app, t)
    await _order(client, app, t, v, h)

    class Rejecting:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            class R:
                status_code = 400

            return R()

    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        out = await analytics.flush(s, app.state.settings, t["id"], client=Rejecting())
    assert out["failed"] == 1
    async with app.state.platform_db.engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT status, error FROM analytics_events WHERE tenant_id = :t"),
                    {"t": t["id"]},
                )
            )
            .mappings()
            .first()
        )
    assert row["status"] == "failed" and "Meta" in row["error"]
