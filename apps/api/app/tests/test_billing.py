import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.modules.billing import service
from app.tests.conftest import make_tenant


def _h(t):
    return {"authorization": f"Bearer {t['staff_token']}", "host": t["primary_host"]}


async def _tenant(client, app, platform_headers, **kw):
    return await make_tenant(
        client, app, platform_headers, slug=f"bill{uuid.uuid4().hex[:6]}", name="Bill shop", **kw
    )


def test_dunning_timeline():
    due = date(2026, 10, 1)
    assert service.dunning_state(None, due) == "current"
    assert service.dunning_state(due, due) == "current"
    assert service.dunning_state(due, due + timedelta(days=3)) == "overdue"
    assert service.dunning_state(due, due + timedelta(days=7)) == "past_due"
    assert service.dunning_state(due, due + timedelta(days=14)) == "suspended"


def test_add_months_clamps_month_end():
    assert service.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert service.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)


async def test_new_tenant_starts_trial_with_starter_limits(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    b = (await client.get("/api/v1/admin/billing", headers=_h(t))).json()
    assert b["plan_code"] == "starter" and b["status"] == "trial"
    assert b["limits"]["custom_domains"] == 1 and b["limits"]["multi_vendor"] is False


async def test_multi_vendor_requires_plan(client, app, platform_headers):
    r = await client.post(
        "/platform/v1/tenants",
        headers=platform_headers,
        json={
            "slug": f"mv{uuid.uuid4().hex[:6]}",
            "name": "Multi",
            "store_mode": "multi",
            "owner_email": "o@example.com",
        },
    )
    assert r.status_code == 402
    t = await _tenant(client, app, platform_headers)
    assert (
        await client.patch(
            f"/platform/v1/tenants/{t['id']}",
            headers=platform_headers,
            json={"store_mode": "multi"},
        )
    ).status_code == 402
    await client.patch(
        f"/platform/v1/tenants/{t['id']}/subscription",
        headers=platform_headers,
        json={"plan_code": "growth"},
    )
    assert (
        await client.patch(
            f"/platform/v1/tenants/{t['id']}",
            headers=platform_headers,
            json={"store_mode": "multi"},
        )
    ).status_code == 200


async def test_quota_blocks_second_custom_domain(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    ok = await client.post(
        "/api/v1/admin/domains", headers=_h(t), json={"host": f"a{uuid.uuid4().hex[:6]}.com"}
    )
    assert ok.status_code == 201
    over = await client.post(
        "/api/v1/admin/domains", headers=_h(t), json={"host": f"b{uuid.uuid4().hex[:6]}.com"}
    )
    assert over.status_code == 402 and over.json()["title"] == "plan_limit"


async def test_invoice_generation_trial_conversion_gmv_fee_idempotency(
    app, client, platform_headers
):
    t = await _tenant(client, app, platform_headers)
    tid = t["id"]
    start = date(2026, 1, 1)
    async with app.state.platform_db.sessionmaker() as db, db.begin():
        # trial ended: first cycle converts to active and bills only usage (none)
        await db.execute(
            text(
                "UPDATE tenant_subscriptions SET current_period_start=:s, current_period_end=:e "
                "WHERE tenant_id=:t"
            ),
            {"s": start - timedelta(days=14), "e": start, "t": tid},
        )
        assert await service.generate_invoice(db, tid, today=start) is None
        sub = (
            await db.execute(
                text(
                    "SELECT status, current_period_start, current_period_end FROM "
                    "tenant_subscriptions WHERE tenant_id=:t"
                ),
                {"t": tid},
            )
        ).one()
        assert sub.status == "active" and sub.current_period_start == start
        assert sub.current_period_end == date(2026, 2, 1)
        # usage within the period, recorded idempotently
        assert await service.record_usage(
            db, tid, "gmv", date(2026, 1, 10), Decimal("100000.00"), "order:1"
        )
        assert not await service.record_usage(
            db, tid, "gmv", date(2026, 1, 10), Decimal("100000.00"), "order:1"
        )
        await service.record_usage(
            db, tid, "gmv", date(2026, 1, 20), Decimal("33333.33"), "order:2"
        )
        await service.record_usage(
            db, tid, "gmv", date(2026, 2, 1), Decimal("999.00"), "order:next-period"
        )

        inv = await service.generate_invoice(
            db, tid, today=date(2026, 2, 1), vat_rate=Decimal("0.15")
        )
        assert inv is not None
        again = await service.generate_invoice(db, tid, today=date(2026, 2, 1))
        assert again is None
        lines = {
            r.kind: r.amount
            for r in (
                await db.execute(
                    text("SELECT kind, amount FROM platform_invoice_lines WHERE invoice_id=:i"),
                    {"i": inv["id"]},
                )
            )
        }
        # starter: 1500 subscription + 5000 setup + 1.5% of 133,333.33 = 2000.00 (half-up)
        assert lines == {
            "subscription": Decimal("1500.00"),
            "setup_fee": Decimal("5000.00"),
            "gmv_fee": Decimal("2000.00"),
        }
        row = (
            await db.execute(
                text("SELECT subtotal, vat_amount, total FROM platform_invoices WHERE id=:i"),
                {"i": inv["id"]},
            )
        ).one()
        assert row.subtotal == Decimal("8500.00") and row.vat_amount == Decimal("1275.00")
        assert row.total == Decimal("9775.00")
        # setup fee only once
        inv2 = await service.generate_invoice(db, tid, today=date(2026, 3, 1))
        kinds = (
            (
                await db.execute(
                    text("SELECT kind FROM platform_invoice_lines WHERE invoice_id=:i"),
                    {"i": inv2["id"]},
                )
            )
            .scalars()
            .all()
        )
        assert "setup_fee" not in kinds


async def test_dunning_suspends_then_payment_restores(app, client, platform_headers):
    t = await _tenant(client, app, platform_headers)
    tid, h, host = t["id"], _h(t), t["primary_host"]
    today = date.today()
    async with app.state.platform_db.sessionmaker() as db, db.begin():
        await db.execute(
            text(
                "UPDATE tenant_subscriptions SET status='active', current_period_start=:s, "
                "current_period_end=:e WHERE tenant_id=:t"
            ),
            {"s": today - timedelta(days=51), "e": today - timedelta(days=21), "t": tid},
        )
        inv = await service.generate_invoice(db, tid, today=today - timedelta(days=21))
        assert (
            await service.reconcile_status(db, tid, today - timedelta(days=12)) == "active"
        )  # overdue grace
        assert await service.reconcile_status(db, tid, today - timedelta(days=7)) == "past_due"
        assert await service.reconcile_status(db, tid, today) == "suspended"
    await client.post(
        "/platform/v1/billing/run-cycle", headers=platform_headers, json={}
    )  # flushes host cache

    # suspended: reads work, writes are locked, billing and sign-in still work
    assert (await client.get("/api/v1/admin/settings", headers=h)).status_code == 200
    r = await client.patch("/api/v1/admin/settings", headers=h, json={"name": "Nope"})
    assert r.status_code == 423
    assert (await client.get("/api/v1/store", headers={"host": host})).json()[
        "status"
    ] == "suspended"
    invoices = (await client.get("/api/v1/admin/billing/invoices", headers=h)).json()
    assert invoices[0]["status"] == "open" and invoices[0]["lines"]

    paid = await client.post(
        f"/platform/v1/invoices/{inv['id']}/mark-paid",
        headers=platform_headers,
        json={"method": "bkash", "reference": "TRX8K2M1"},
    )
    assert paid.json()["changed"] is True
    again = await client.post(
        f"/platform/v1/invoices/{inv['id']}/mark-paid",
        headers=platform_headers,
        json={"method": "bkash", "reference": "TRX8K2M1"},
    )
    assert again.json()["changed"] is False
    assert (
        await client.patch("/api/v1/admin/settings", headers=h, json={"name": "Back"})
    ).status_code == 200


async def test_billing_is_owner_only_and_tenant_scoped(
    client, app, platform_headers, world, settings
):
    from sqlalchemy.ext.asyncio import create_async_engine

    A = world["A"]
    assert (
        await client.get(
            f"/api/v1/admin/billing/invoices/{A.invoice_id}", headers=A.staff.headers()
        )
    ).status_code == 200
    eng = create_async_engine(settings.database_url)
    try:
        async with eng.connect() as conn, conn.begin():
            await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": A.id})
            ids = (
                (await conn.execute(text("SELECT tenant_id FROM platform_invoices")))
                .scalars()
                .all()
            )
            assert ids and all(str(i) == A.id for i in ids)
            with pytest.raises(Exception):  # noqa: B017 - app role may not write billing
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE platform_invoices SET status='paid'"))
    finally:
        await eng.dispose()
