"""Phase 18: what the app asks the server.

A white-label app should be able to rebrand, retheme and reshuffle its home screen without a store
submission, should refuse to run when it is too old to be safe, and must be able to delete an
account without deleting the tenant's sales records.
"""

import uuid

from sqlalchemy import text

from app.modules.mobile import service as mobile
from app.modules.mobile.router import update_state
from app.tests.test_checkout import ADDRESS, _buyer, _market, _place, _vendor
from app.tests.test_payments import _configure, _gateway


async def _shop(client, app, platform_headers, slug):
    await _gateway(app)
    t, staff, cat = await _market(client, app, platform_headers)
    v = await _vendor(client, app, t, staff, slug, cat, price="1000", stock=5)
    await _configure(client, staff)
    return t, staff, v


def _host(t):
    return {"host": t["primary_host"]}


async def test_the_app_gets_its_whole_face_from_one_call(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "app-alpha")
    await client.put(
        "/api/v1/admin/app-config",
        headers=staff,
        json={
            "app": "buyer",
            "app_name": "Rongin Bazar",
            "bundle_id": "com.rongin.buyer",
            "crash_dsn": "https://sentry.example/1",
            "features": {"wishlist": True},
        },
    )
    config = (await client.get("/api/v1/app/config", headers=_host(t))).json()
    assert config["branding"]["app_name"] == "Rongin Bazar"
    assert config["theme"]["tokens"]  # the published theme drives the app's colours too
    assert config["payments"]["cod"] is True and "bkash" in config["payments"]["gateways"]
    assert config["features"]["wishlist"] is True and config["features"]["messaging"] is True
    assert config["crash_dsn"].startswith("https://")
    assert config["update"]["state"] == "ok" and config["maintenance"]["on"] is False

    home = await client.get("/api/v1/app/home", headers=_host(t))
    assert home.status_code == 200 and "sections" in home.json()
    # one home, two clients: the app renders the same sections the web storefront does
    web = await client.get("/api/v1/storefront/page?template=home", headers=_host(t))
    assert [s["type"] for s in home.json()["sections"]] == [
        s["type"] for s in web.json()["sections"]
    ]


async def test_an_old_app_is_told_to_update_and_a_very_old_one_is_stopped(
    client, app, platform_headers
):
    t, staff, v = await _shop(client, app, platform_headers, "app-beta")
    assert (
        await client.put(
            "/api/v1/admin/app-releases",
            headers=staff,
            json={
                "platform": "android",
                "latest_version": "2.4.0",
                "min_supported_version": "2.0.0",
                "store_url": "https://play.google.com/store/apps/details?id=com.rongin.buyer",
                "force_message": "Please update to keep ordering.",
            },
        )
    ).status_code == 200

    def state(version):
        return version, None

    current = (
        await client.get("/api/v1/app/config?platform=android&version=2.4.0", headers=_host(t))
    ).json()
    assert current["update"]["state"] == "ok"
    older = (
        await client.get("/api/v1/app/config?platform=android&version=2.3.9", headers=_host(t))
    ).json()
    assert older["update"]["state"] == "optional" and older["update"]["latest_version"] == "2.4.0"
    ancient = (
        await client.get("/api/v1/app/config?platform=android&version=1.9.9", headers=_host(t))
    ).json()
    assert ancient["update"]["state"] == "force"
    assert "update" in ancient["update"]["message"].lower()
    assert ancient["update"]["store_url"].startswith("https://play.google.com")

    # a minimum newer than the latest would lock everyone out
    assert (
        await client.put(
            "/api/v1/admin/app-releases",
            headers=staff,
            json={
                "platform": "ios",
                "latest_version": "1.0.0",
                "min_supported_version": "2.0.0",
            },
        )
    ).status_code == 422


def test_versions_compare_as_numbers_not_strings():
    # the classic bug: "1.10.0" < "1.9.0" as text, which locks out the newest build
    assert update_state("1.10.0", "1.10.0", "1.9.0") == "ok"
    assert update_state("1.9.0", "1.10.0", "1.9.0") == "optional"
    assert update_state("1.8.9", "1.10.0", "1.9.0") == "force"
    assert (
        update_state(None, "1.10.0", "1.9.0") == "ok"
    )  # a client that does not say cannot be judged
    assert update_state("2", "2.0.0", "1.0.0") == "ok"


async def test_maintenance_mode_speaks_for_the_whole_app(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "app-gamma")
    await client.put(
        "/api/v1/admin/app-config",
        headers=staff,
        json={
            "app": "buyer",
            "maintenance": True,
            "maintenance_message": "আমরা একটু পরেই ফিরছি",
        },
    )
    config = (await client.get("/api/v1/app/config", headers=_host(t))).json()
    assert config["maintenance"] == {"on": True, "message": "আমরা একটু পরেই ফিরছি"}


# ------------------------------------------------------------------------- account deletion
async def test_deleting_an_account_keeps_the_sales_record(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "app-delta")
    h = await _buyer(client, app, t, phone_verified=True)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": 1}
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

    # an order still in flight blocks deletion, with a reason the buyer can act on
    blocked = await client.post("/api/v1/app/account/delete", headers=h, json={})
    assert blocked.status_code == 409 and blocked.json()["title"] == "open_orders"

    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE sub_orders SET status = 'delivered' WHERE tenant_id = :t"), {"t": t["id"]}
        )
    requested = await client.post(
        "/api/v1/app/account/delete", headers=h, json={"reason": "Moving abroad"}
    )
    assert requested.status_code == 202
    assert requested.json()["grace_days"] == 14
    # asking twice does not stack requests
    assert (await client.post("/api/v1/app/account/delete", headers=h, json={})).status_code == 202
    assert (await client.get("/api/v1/app/account/delete", headers=h)).json()["status"] == "pending"
    # and it can be called off within the grace period
    assert (await client.post("/api/v1/app/account/delete/cancel", headers=h)).json()[
        "status"
    ] == "cancelled"
    assert (await client.post("/api/v1/app/account/delete/cancel", headers=h)).status_code == 404

    await client.post("/api/v1/app/account/delete", headers=h, json={})
    queue = (await client.get("/api/v1/admin/account-deletions", headers=staff)).json()
    pending = next(x for x in queue if x["status"] == "pending")
    assert (
        await client.post(
            f"/api/v1/admin/account-deletions/{pending['id']}/complete", headers=staff
        )
    ).status_code == 204

    async with app.state.platform_db.engine.begin() as conn:
        user = (
            (
                await conn.execute(
                    text(
                        "SELECT email, phone, full_name, status FROM users WHERE tenant_id = :t "
                        "AND email LIKE 'deleted-%'"
                    ),
                    {"t": t["id"]},
                )
            )
            .mappings()
            .first()
        )
        kept = (
            (
                await conn.execute(
                    text(
                        """SELECT o.number, o.grand_total, o.contact_phone,
                              o.shipping_address->>'recipient_name' AS name
                       FROM orders o WHERE o.tenant_id = :t AND o.number = :n"""
                    ),
                    {"t": t["id"], "n": order["number"]},
                )
            )
            .mappings()
            .one()
        )
        addresses = await conn.scalar(
            text("SELECT count(*) FROM addresses WHERE tenant_id = :t"), {"t": t["id"]}
        )
        tokens = await conn.scalar(
            text(
                """SELECT count(*) FROM refresh_tokens r
                   JOIN users u ON u.id = r.user_id AND u.tenant_id = r.tenant_id
                   WHERE r.tenant_id = :t AND r.revoked_at IS NULL AND u.status = 'deleted'"""
            ),
            {"t": t["id"]},
        )
        ledger = await conn.scalar(
            text("SELECT count(*) FROM ledger_entries WHERE tenant_id = :t"), {"t": t["id"]}
        )
    assert user["status"] == "deleted" and user["phone"] is None
    assert user["full_name"] == "Deleted account"
    # the sale itself survives, with the person scrubbed out of it
    assert kept["number"] == order["number"] and kept["grand_total"] is not None
    assert kept["contact_phone"] == "deleted" and kept["name"] == "Deleted account"
    assert addresses == 0 and tokens == 0 and ledger > 0


async def test_the_scheduled_job_only_takes_what_is_due(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "app-epsilon")
    h = await _buyer(client, app, t, phone_verified=True)
    await client.post("/api/v1/app/account/delete", headers=h, json={})
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        assert await mobile.run_due_deletions(s, t["id"]) == 0  # the grace period is still running
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE account_deletion_requests SET scheduled_for = now() - interval '1 day' "
                "WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        assert await mobile.run_due_deletions(s, t["id"]) == 1
        assert await mobile.run_due_deletions(s, t["id"]) == 0  # and never twice
    async with app.state.platform_db.engine.begin() as conn:
        audited = await conn.scalar(
            text(
                "SELECT count(*) FROM audit_log WHERE tenant_id = :t AND action = 'account.anonymised'"
            ),
            {"t": t["id"]},
        )
    assert audited == 1


async def test_a_device_token_follows_the_person_not_the_phone(client, app, platform_headers):
    t, staff, v = await _shop(client, app, platform_headers, "app-zeta")
    token = f"tok-{uuid.uuid4().hex}"
    first = await _buyer(client, app, t)
    second = await _buyer(client, app, t)
    await client.post(
        "/api/v1/me/devices", headers=first, json={"token": token, "platform": "android"}
    )
    # the same handset, a different person signing in: the token must move, not duplicate
    await client.post(
        "/api/v1/me/devices", headers=second, json={"token": token, "platform": "android"}
    )
    async with app.state.platform_db.engine.begin() as conn:
        rows = (
            await conn.execute(
                text("SELECT user_id FROM device_tokens WHERE tenant_id = :t AND token = :tok"),
                {"t": t["id"], "tok": token},
            )
        ).all()
    assert len(rows) == 1
