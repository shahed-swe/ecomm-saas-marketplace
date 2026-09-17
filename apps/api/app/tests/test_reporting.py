"""Phase 17: the numbers a tenant runs the business on.

Reports are where quiet wrongness hides: a vendor seeing someone else's GMV, a rollup that counts
a day twice, a dashboard that says zero because the nightly job has not run, or an export CSV
sitting on a public URL with three hundred customers' phone numbers in it.
"""

import csv
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal as D

from sqlalchemy import text

from app.modules.reporting import service as reporting
from app.tests.test_checkout import ADDRESS, _buyer, _market, _place, _vendor
from app.tests.test_fulfilment import _configure_courier, _courier, _webhook
from app.tests.test_payments import _configure, _gateway


async def _shop(client, app, platform_headers, slug, vendors=1):
    await _gateway(app)
    await _courier(app)
    t, staff, cat = await _market(client, app, platform_headers)
    made = [
        await _vendor(client, app, t, staff, f"{slug}-{i}", cat, price="1000", stock=20, weight=500)
        for i in range(vendors)
    ]
    await _configure(client, staff)
    await _configure_courier(client, staff)
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_settings SET default_commission_rate = 0.10 WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    return t, staff, made


async def _sale(client, app, t, v, *, qty=1, deliver=False, method="bkash"):
    h = await _buyer(client, app, t, phone_verified=True)
    await client.post(
        "/api/v1/cart/items", headers=h, json={"variant_id": v["variant_id"], "qty": qty}
    )
    addr = (await client.post("/api/v1/me/addresses", headers=h, json=ADDRESS)).json()
    q = (await client.post("/api/v1/cart/quote", headers=h, json={"district_code": "dhaka"})).json()
    order = (await _place(client, h, q, address_id=addr["id"], payment_method=method)).json()
    if method != "cod":
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
    return h, order, sub


# ------------------------------------------------------------------------------- the dashboard
async def test_todays_sales_show_up_without_waiting_for_the_nightly_job(
    client, app, platform_headers
):
    t, staff, (v,) = await _shop(client, app, platform_headers, "rep-alpha")
    await _sale(client, app, t, v, qty=2)
    await _sale(client, app, t, v, deliver=True, method="cod")

    report = (await client.get("/api/v1/admin/reports/overview", headers=staff)).json()
    summary = report["summary"]
    assert summary["orders"] == 2 and summary["units"] == 3
    assert D(summary["gmv"]) > 0 and D(summary["commission"]) == D("300.00")
    assert summary["cod_orders"] == 1 and summary["cod_share"] == 0.5
    assert summary["delivered"] == 1
    assert D(summary["average_order_value"]) == D(summary["gmv"]) / 2
    assert (
        report["series"][-1]["day"] == str(reporting._today())
        or report["series"][-1]["day"] == reporting._today()
    )
    assert report["top_products"][0]["units"] == 3


async def test_a_rollup_can_be_run_twice_without_doubling_a_day(client, app, platform_headers):
    t, staff, (v,) = await _shop(client, app, platform_headers, "rep-beta")
    await _sale(client, app, t, v)
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        await reporting.rollup_day(s, t["id"], reporting._today())
        await reporting.rollup_day(s, t["id"], reporting._today())
        await reporting.rollup_range(s, t["id"], days=2)
    async with app.state.platform_db.engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT vendor_id, orders, gmv FROM daily_metrics WHERE tenant_id = :t AND day = :d"
                ),
                {"t": t["id"], "d": reporting._today()},
            )
        ).all()
    totals = [r for r in rows if r[0] is None]
    assert len(totals) == 1 and totals[0][1] == 1  # exactly one tenant-total row, one order
    assert len([r for r in rows if r[0] is not None]) == 1


async def test_a_vendor_sees_only_its_own_slice(client, app, platform_headers):
    t, staff, (a, b) = await _shop(client, app, platform_headers, "rep-gamma", vendors=2)
    await _sale(client, app, t, a, qty=3)
    await _sale(client, app, t, b, qty=1)

    tenant_view = (await client.get("/api/v1/admin/reports/overview", headers=staff)).json()
    assert tenant_view["summary"]["units"] == 4
    assert {x["display_name"] for x in tenant_view["top_vendors"]} == {"Rep-Gamma-0", "Rep-Gamma-1"}

    a_view = (await client.get("/api/v1/vendor/reports/overview", headers=a["h"])).json()
    b_view = (await client.get("/api/v1/vendor/reports/overview", headers=b["h"])).json()
    assert a_view["summary"]["units"] == 3 and b_view["summary"]["units"] == 1
    assert D(a_view["summary"]["gmv"]) > D(b_view["summary"]["gmv"])
    assert "top_vendors" not in a_view  # a vendor never gets a league table of its competitors
    assert {p["title"] for p in a_view["top_products"]} == {"Item rep-gamma-0"}


async def test_refunds_come_off_net_sales(client, app, platform_headers):
    t, staff, (v,) = await _shop(client, app, platform_headers, "rep-delta")
    h, order, sub = await _sale(client, app, t, v, deliver=True)
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
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/decision", headers=v["h"], json={"approve": True}
    )
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/mark", headers=v["h"], json={"status": "received"}
    )
    await client.post(
        f"/api/v1/vendor/returns/{ret['id']}/qc", headers=v["h"], json={"passed": True}
    )
    await client.post(f"/api/v1/admin/returns/{ret['id']}/refund", headers=staff)

    summary = (await client.get("/api/v1/admin/reports/overview", headers=staff)).json()["summary"]
    assert D(summary["refunds"]) == D("1000.00")
    assert D(summary["net_sales"]) == D(summary["gmv"]) - D("1000.00")


# ---------------------------------------------------------------------------------- the exports
async def test_an_export_is_a_private_file_with_the_right_rows(client, app, platform_headers):
    t, staff, (v,) = await _shop(client, app, platform_headers, "rep-epsilon")
    await _sale(client, app, t, v, qty=2)
    await _sale(client, app, t, v)
    export = await client.post("/api/v1/admin/reports/exports?kind=orders", headers=staff)
    assert export.status_code == 201, export.text
    body = export.json()
    assert body["rows"] == 2 and body["url"].startswith("/internal/private/")
    assert body["object_key"].startswith(f"t/{t['id']}/")  # tenant-scoped object key

    downloaded = await client.get(body["url"])
    assert downloaded.status_code == 200
    rows = list(csv.DictReader(io.StringIO(downloaded.text)))
    assert len(rows) == 2 and rows[0]["vendor"] == "Rep-Epsilon-0"
    assert "district" in rows[0]

    listed = (await client.get("/api/v1/admin/reports/exports", headers=staff)).json()
    assert listed[0]["kind"] == "orders" and listed[0]["rows"] == 2
    again = (
        await client.get(f"/api/v1/admin/reports/exports/{listed[0]['id']}/url", headers=staff)
    ).json()
    assert again["url"].startswith("/internal/private/")


async def test_a_vendor_export_contains_only_that_vendor(client, app, platform_headers):
    t, staff, (a, b) = await _shop(client, app, platform_headers, "rep-zeta", vendors=2)
    await _sale(client, app, t, a)
    await _sale(client, app, t, b)
    export = (
        await client.post("/api/v1/vendor/reports/exports?kind=orders", headers=a["h"])
    ).json()
    assert export["rows"] == 1
    rows = list(csv.DictReader(io.StringIO((await client.get(export["url"])).text)))
    assert {r["vendor"] for r in rows} == {"Rep-Zeta-0"}


# --------------------------------------------------------------------------------- the platform
async def test_the_platform_sees_its_own_business_and_nobody_elses_customers(
    client, app, platform_headers
):
    t, staff, (v,) = await _shop(client, app, platform_headers, "rep-eta")
    await _sale(client, app, t, v, qty=2)
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        await reporting.rollup_day(s, t["id"], reporting._today())

    overview = await client.get("/platform/v1/reports/overview", headers=platform_headers)
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert D(body["mrr"]) >= 0 and body["gmv_window_days"] == 30
    mine = next(x for x in body["tenants"] if x["id"] == t["id"])
    # one seller plus the tenant's own house vendor
    assert D(mine["gmv_30d"]) > 0 and mine["vendors"] >= 1 and mine["subscription"]
    assert "customers" not in body and "orders" not in mine  # aggregates only

    # the platform surface does not exist for a tenant's staff
    assert (await client.get("/platform/v1/reports/overview", headers=staff)).status_code == 404


def test_the_window_defaults_to_the_last_thirty_days():
    from app.modules.reporting.router import _window

    start, end = _window(None, None)
    assert (end - start) == timedelta(days=29) and end == reporting._today()
    # a backwards range is read the way it was obviously meant
    a, b = _window(date(2026, 3, 10), date(2026, 3, 1))
    assert a == date(2026, 3, 1) and b == date(2026, 3, 10)
