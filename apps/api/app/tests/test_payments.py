"""Phase 11: the money must be verified, idempotent and tenant-bound.

Every test here exists because of a specific way an e-commerce site loses money:
a forged callback, a retried IPN, a short payment, another tenant's webhook, a paid order whose
stock was never taken out of reserve.
"""

import uuid
from decimal import Decimal as D

from sqlalchemy import text

from app.modules.payments import service
from app.modules.payments.gateways import CallbackEvent, CreatedPayment, GatewayError
from app.modules.payments.gateways import VerifiedPayment as VP
from app.tests.test_checkout import ADDRESS, _buyer, _market, _place, _vendor

BKASH_CREDS = {
    "app_key": "appkey12345",
    "app_secret": "secret",
    "username": "sandboxUser",
    "password": "hunter2",
}


class FakeGateway:
    """A gateway that answers exactly what the test tells it to — never what the callback claims."""

    name = "bkash"

    def __init__(self):
        self.amounts: dict[str, D] = {}
        self.result = "paid"
        self.amount_override: D | None = None
        self.healthy = True
        self.verify_calls = 0
        self.raise_on_create = False

    async def create(
        self, *, credentials, amount, order_number, return_url, callback_url, buyer_phone
    ):
        if self.raise_on_create:
            raise GatewayError("provider down")
        ref = f"TR{uuid.uuid4().hex[:10].upper()}"
        self.amounts[ref] = D(str(amount))
        self.last_callback_url = callback_url
        return CreatedPayment(
            provider_ref=ref, redirect_url=f"https://pay.example/{ref}", raw={"paymentID": ref}
        )

    async def verify(self, *, credentials, provider_ref):
        self.verify_calls += 1
        amount = self.amount_override or self.amounts.get(provider_ref)
        if self.result != "paid":
            return VP(status=self.result, failure_reason="declined by user")
        return VP(status="paid", amount=amount, payer_ref="01799999999")

    def parse_callback(self, *, credentials, body, form, headers):
        ref = form.get("paymentID")
        if not ref:
            raise GatewayError("callback without paymentID")
        status = form.get("status", "unknown")
        return CallbackEvent(
            event_id=f"{ref}:{status}", provider_ref=ref, kind=f"bkash.{status}", payload=dict(form)
        )

    async def refund(self, *, credentials, provider_ref, amount, reason):
        return {"ok": True}

    async def health(self, *, credentials):
        if not self.healthy:
            raise GatewayError("credentials rejected")


async def _gateway(app):
    fake = FakeGateway()
    app.state.payment_gateways = {"bkash": fake, "sslcommerz": fake}
    return fake


async def _configure(client, staff, provider="bkash", creds=None, mode="sandbox"):
    r = await client.put(
        "/api/v1/admin/payment-accounts",
        headers=staff,
        json={"provider": provider, "mode": mode, "credentials": creds or BKASH_CREDS},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _public_id(app, tenant_id: str) -> str:
    async with app.state.platform_db.engine.begin() as conn:
        return (
            await conn.execute(
                text("SELECT public_id FROM tenants WHERE id = :t"), {"t": tenant_id}
            )
        ).scalar()


async def _shop(client, app, platform_headers, slug: str):
    t, staff, cat = await _market(client, app, platform_headers)
    v = await _vendor(client, app, t, staff, slug, cat, price="1000", stock=5, weight=500)
    return t, staff, v


async def _order(client, app, t, v, *, method="bkash", phone_verified=False, qty=1):
    h = await _buyer(client, app, t, phone_verified=phone_verified)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": qty}
    )
    addr = (await client.post("/api/v1/me/addresses", headers=h, json=ADDRESS)).json()
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    r = await _place(client, h, q, address_id=addr["id"], payment_method=method)
    assert r.status_code == 201, r.text
    return h, r.json()


# ------------------------------------------------------------------------------- happy path
async def test_prepaid_payment_settles_once_and_consumes_reserved_stock(
    client, app, platform_headers
):
    fake = await _gateway(app)
    t, staff, v = await _shop(client, app, platform_headers, "pay-alpha")
    await _configure(client, staff)
    h, order = await _order(client, app, t, v, qty=2)

    start = await client.post(
        "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
    )
    assert start.status_code == 200, start.text
    payment_id = start.json()["payment_id"]
    assert start.json()["redirect_url"].startswith("https://pay.example/")
    assert f"/webhooks/bkash/{await _public_id(app, t['id'])}" in fake.last_callback_url

    # before confirmation nothing has moved
    detail = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    assert detail["variants"][0]["stock_on_hand"] == 5 and detail["variants"][0]["available"] == 3
    assert (await client.get(f"/api/v1/me/orders/{order['number']}", headers=h)).json()[
        "status"
    ] == "pending_payment"

    done = await client.post(f"/api/v1/payments/{payment_id}/confirm", headers=h)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "paid" and done.json()["settled"] is True
    assert (await client.get(f"/api/v1/me/orders/{order['number']}", headers=h)).json()[
        "status"
    ] == "processing"

    detail = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    assert detail["variants"][0]["stock_on_hand"] == 3  # reserved stock became sold stock
    assert detail["variants"][0]["available"] == 3
    async with app.state.platform_db.engine.begin() as conn:
        moves = (
            await conn.execute(
                text(
                    "SELECT delta, balance_after, reason FROM inventory_movements "
                    "WHERE tenant_id = :t AND reason = 'order'"
                ),
                {"t": t["id"]},
            )
        ).all()
    assert moves == [(-2, 3, "order")]

    # a second confirmation settles nothing twice
    again = await client.post(f"/api/v1/payments/{payment_id}/confirm", headers=h)
    assert again.json()["status"] == "paid" and again.json()["settled"] is False
    assert fake.verify_calls == 1
    detail = (await client.get(f"/api/v1/vendor/products/{v['product_id']}", headers=v["h"])).json()
    assert detail["variants"][0]["stock_on_hand"] == 3

    # and a paid order cannot open another gateway session
    retry = await client.post(
        "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
    )
    assert retry.status_code == 409 and retry.json()["title"] == "already_paid"


# ------------------------------------------------------------------------- callbacks and replays
async def test_callback_is_a_nudge_not_evidence(client, app, platform_headers):
    fake = await _gateway(app)
    t, staff, v = await _shop(client, app, platform_headers, "pay-beta")
    await _configure(client, staff)
    h, order = await _order(client, app, t, v)
    start = (
        await client.post(
            "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
        )
    ).json()
    provider_ref = next(iter(fake.amounts))
    hook = f"/webhooks/bkash/{await _public_id(app, t['id'])}"

    # the buyer's browser claims success; the provider says the payment failed
    fake.result = "failed"
    r = await client.post(hook, data={"paymentID": provider_ref, "status": "success"})
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert (await client.get(f"/api/v1/me/orders/{order['number']}", headers=h)).json()[
        "status"
    ] == "pending_payment"

    # the same event replayed changes nothing and does not call the provider again
    calls = fake.verify_calls
    dup = await client.post(hook, data={"paymentID": provider_ref, "status": "success"})
    assert dup.json()["status"] == "duplicate" and fake.verify_calls == calls

    # when the provider finally says paid, the order is paid — verified, not claimed
    fake.result = "paid"
    ok = await client.post(hook, data={"paymentID": provider_ref, "status": "retry"})
    assert ok.json()["status"] == "paid"
    assert (await client.get(f"/api/v1/payments/{start['payment_id']}", headers=h)).json()[
        "status"
    ] == "paid"


async def test_amount_mismatch_never_pays_the_order(client, app, platform_headers):
    fake = await _gateway(app)
    t, staff, v = await _shop(client, app, platform_headers, "pay-gamma")
    await _configure(client, staff)
    h, order = await _order(client, app, t, v)
    start = (
        await client.post(
            "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
        )
    ).json()
    fake.amount_override = D("1.00")  # a rupee for a thousand-taka order
    r = await client.post(f"/api/v1/payments/{start['payment_id']}/confirm", headers=h)
    assert r.json()["status"] == "failed" and r.json()["settled"] is False
    assert (await client.get(f"/api/v1/me/orders/{order['number']}", headers=h)).json()[
        "status"
    ] == "pending_payment"
    rows = (await client.get("/api/v1/admin/payments", headers=staff)).json()
    assert rows[0]["failure_reason"] == "amount_mismatch"


async def test_webhook_for_another_tenant_cannot_touch_this_order(client, app, platform_headers):
    fake = await _gateway(app)
    a_t, a_staff, a_v = await _shop(client, app, platform_headers, "pay-delta")
    b_t, b_staff, _ = await _shop(client, app, platform_headers, "pay-epsilon")
    await _configure(client, a_staff)
    await _configure(client, b_staff)
    h, order = await _order(client, app, a_t, a_v)
    await client.post("/api/v1/payments/start", headers=h, json={"order_number": order["number"]})
    provider_ref = next(iter(fake.amounts))

    # tenant B's webhook, tenant A's payment reference: B's session sees nothing of A's
    r = await client.post(
        f"/webhooks/bkash/{await _public_id(app, b_t['id'])}",
        data={"paymentID": provider_ref, "status": "success"},
    )
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert (await client.get(f"/api/v1/me/orders/{order['number']}", headers=h)).json()[
        "status"
    ] == "pending_payment"
    # an unknown tenant id is simply not found
    assert (
        await client.post("/webhooks/bkash/deadbeef", data={"paymentID": "x"})
    ).status_code == 404
    # and an unconfigured provider does not reveal whether the tenant exists
    assert (await client.post("/webhooks/nagad/deadbeef", data={})).status_code == 404


# --------------------------------------------------------------------------- cash on delivery
async def test_cod_creates_receivables_that_cancel_with_the_order(client, app, platform_headers):
    await _gateway(app)
    t, staff, v = await _shop(client, app, platform_headers, "pay-zeta")
    h, order = await _order(client, app, t, v, method="cod", phone_verified=True)

    due = (await client.get("/api/v1/vendor/cod-receivables", headers=v["h"])).json()
    assert len(due) == 1 and due[0]["status"] == "due" and due[0]["order_number"] == order["number"]
    assert D(due[0]["amount"]) > 0
    assert (await client.get("/api/v1/admin/cod-receivables", headers=staff)).json()[0][
        "status"
    ] == "due"
    # a prepaid order cannot be started for a COD order
    assert (
        await client.post(
            "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
        )
    ).status_code == 409

    # courier delivers and collects: the COD payment becomes paid
    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            sub_id = (
                await s.execute(
                    text(
                        "SELECT s.id FROM sub_orders s JOIN orders o ON o.id = s.order_id "
                        "WHERE s.tenant_id = :t AND o.number = :n"
                    ),
                    {"t": t["id"], "n": order["number"]},
                )
            ).scalar()
            assert await service.collect_cod(s, t["id"], sub_id, courier="pathao") is True
            assert await service.collect_cod(s, t["id"], sub_id) is False  # idempotent
    paid = (await client.get("/api/v1/admin/payments", headers=staff)).json()
    assert paid[0]["provider"] == "cod" and paid[0]["status"] == "paid"
    assert (await client.get("/api/v1/vendor/cod-receivables", headers=v["h"])).json()[0][
        "status"
    ] == "collected"


async def test_cancelled_cod_order_cancels_its_receivable(client, app, platform_headers):
    await _gateway(app)
    t, staff, v = await _shop(client, app, platform_headers, "pay-eta")
    h, order = await _order(client, app, t, v, method="cod", phone_verified=True)
    from app.modules.checkout.service import release_order

    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            oid = (
                await s.execute(
                    text("SELECT id FROM orders WHERE tenant_id = :t AND number = :n"),
                    {"t": t["id"], "n": order["number"]},
                )
            ).scalar()
            assert (
                await release_order(s, t["id"], oid, "staff_cancel", statuses=("confirmed",)) == 1
            )
    receivables = (await client.get("/api/v1/vendor/cod-receivables", headers=v["h"])).json()
    assert receivables[0]["status"] == "cancelled"
    assert (await client.get("/api/v1/admin/payments", headers=staff)).json()[0][
        "status"
    ] == "cancelled"


# --------------------------------------------------------------------------------- credentials
async def test_credentials_are_encrypted_masked_and_health_checked(client, app, platform_headers):
    fake = await _gateway(app)
    t, staff, v = await _shop(client, app, platform_headers, "pay-theta")
    view = await _configure(client, staff)
    assert view["status"] == "healthy" and view["key_hint"] == "••••2345"
    assert "app_secret" not in str(view)

    async with app.state.platform_db.engine.begin() as conn:
        stored = (
            await conn.execute(
                text("SELECT credentials_ciphertext FROM payment_accounts WHERE tenant_id = :t"),
                {"t": t["id"]},
            )
        ).scalar()
        logged = (
            await conn.execute(
                text(
                    "SELECT data::text FROM audit_log WHERE tenant_id = :t AND action = 'payment_account.set'"
                ),
                {"t": t["id"]},
            )
        ).scalar()
    assert "hunter2" not in stored and "appkey12345" not in stored and stored.startswith("v1:")
    assert "hunter2" not in logged

    fake.healthy = False
    bad = await client.post(
        "/api/v1/admin/payment-accounts/health", headers=staff, json={"provider": "bkash"}
    )
    assert bad.json()["status"] == "failing" and "rejected" in bad.json()["last_error"]

    listed = (await client.get("/api/v1/admin/payment-accounts", headers=staff)).json()
    assert [a["provider"] for a in listed] == ["bkash"] and listed[0]["key_hint"] == "••••2345"

    assert (
        await client.put(
            "/api/v1/admin/payment-accounts",
            headers=staff,
            json={"provider": "bkash", "credentials": {"app_key": "only"}},
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/v1/admin/payment-accounts/disable", headers=staff, json={"provider": "bkash"}
        )
    ).status_code == 204
    h, order = await _order(client, app, t, v)
    r = await client.post(
        "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
    )
    assert r.status_code == 409 and r.json()["title"] == "gateway_disabled"


async def _vendor_of(client, app, t, staff):
    cat = (
        await client.get("/api/v1/catalog/categories", headers={"host": t["primary_host"]})
    ).json()
    cat_id = cat[0]["id"] if isinstance(cat, list) else cat["items"][0]["id"]
    return await _vendor(client, app, t, staff, f"extra-{uuid.uuid4().hex[:5]}", cat_id)


async def test_payment_belongs_to_its_buyer(client, app, platform_headers):
    await _gateway(app)
    t, staff, v = await _shop(client, app, platform_headers, "pay-iota")
    await _configure(client, staff)
    h, order = await _order(client, app, t, v)
    start = (
        await client.post(
            "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
        )
    ).json()
    other = await _buyer(client, app, t)
    assert (
        await client.post(
            "/api/v1/payments/start", headers=other, json={"order_number": order["number"]}
        )
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/payments/{start['payment_id']}/confirm", headers=other)
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/payments/{start['payment_id']}", headers=other)
    ).status_code == 404


# ------------------------------------------------------------------------------ reconciliation
async def test_abandoned_attempt_is_reconciled_from_the_provider(client, app, platform_headers):
    fake = await _gateway(app)
    t, staff, v = await _shop(client, app, platform_headers, "pay-kappa")
    await _configure(client, staff)
    h, order = await _order(client, app, t, v)
    await client.post("/api/v1/payments/start", headers=h, json={"order_number": order["number"]})
    # the buyer paid on the bKash app and then closed the tab: no callback, no confirm
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE payments SET updated_at = now() - interval '1 hour' WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            result = await service.reconcile_pending(
                s, app.state.settings, tenant_id=t["id"], overrides={"bkash": fake}
            )
    assert result == {"checked": 1, "paid": 1, "closed": 0, "unresolved": 0}
    assert (await client.get(f"/api/v1/me/orders/{order['number']}", headers=h)).json()[
        "status"
    ] == "processing"

    summary = (await client.get("/api/v1/admin/payments/reconciliation", headers=staff)).json()
    paid = [g for g in summary["gateways"] if g["status"] == "paid"]
    assert paid and paid[0]["provider"] == "bkash" and D(paid[0]["amount"]) > 0
    assert summary["events_without_payment"] == 0

    # a second sweep has nothing left to do
    async with app.state.db.sessionmaker() as s:
        async with s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            assert (
                await service.reconcile_pending(
                    s, app.state.settings, tenant_id=t["id"], overrides={"bkash": fake}
                )
            )["checked"] == 0


async def test_unmatched_callback_is_stored_for_reconciliation(client, app, platform_headers):
    await _gateway(app)
    t, staff, _ = await _shop(client, app, platform_headers, "pay-lambda")
    await _configure(client, staff)
    r = await client.post(
        f"/webhooks/bkash/{await _public_id(app, t['id'])}",
        data={"paymentID": "TRNOTOURS", "status": "success"},
    )
    assert r.json()["status"] == "ignored"
    summary = (await client.get("/api/v1/admin/payments/reconciliation", headers=staff)).json()
    assert summary["events_without_payment"] == 1


async def test_return_page_can_find_the_attempt_by_order_number(client, app, platform_headers):
    await _gateway(app)
    t, staff, v = await _shop(client, app, platform_headers, "pay-mu")
    await _configure(client, staff)
    h, order = await _order(client, app, t, v)
    assert (await client.get(f"/api/v1/me/orders/{order['number']}/payment", headers=h)).json()[
        "status"
    ] == "none"
    start = (
        await client.post(
            "/api/v1/payments/start", headers=h, json={"order_number": order["number"]}
        )
    ).json()
    found = (await client.get(f"/api/v1/me/orders/{order['number']}/payment", headers=h)).json()
    assert found["id"] == start["payment_id"] and found["status"] == "pending"
    other = await _buyer(client, app, t)
    assert (
        await client.get(f"/api/v1/me/orders/{order['number']}/payment", headers=other)
    ).status_code == 404
