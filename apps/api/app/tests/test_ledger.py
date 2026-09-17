"""Phase 14: the books.

The ledger is the only place that can answer "who is owed what". So these tests check the things
that would quietly make that answer wrong: a group that does not balance, an event posted twice,
commission recomputed at today's rate instead of the snapshot, a payout paid twice, a reserve that
releases too early, or a tax invoice number with a gap in it.
"""

import uuid
from datetime import date
from decimal import Decimal as D

from sqlalchemy import text

from app.modules.ledger import accounts as acct
from app.modules.ledger import documents, payouts
from app.modules.ledger import service as ledger
from app.tests.conftest import login, set_password
from app.tests.test_checkout import ADDRESS, _buyer, _market, _place, _vendor
from app.tests.test_fulfilment import _configure_courier, _courier, _webhook
from app.tests.test_payments import _configure, _gateway


async def _finance_shop(client, app, platform_headers, slug, **settings):
    await _gateway(app)
    await _courier(app)
    t, staff, cat = await _market(client, app, platform_headers, **settings)
    v = await _vendor(client, app, t, staff, slug, cat, price="1000", stock=10, weight=600)
    await _configure(client, staff)
    await _configure_courier(client, staff)
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_settings SET default_commission_rate = 0.10 WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    return t, staff, v


async def _pay_and_deliver(client, app, t, v, *, method="bkash", qty=1, deliver=True):
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
    consignment = None
    if deliver:
        shipment = (
            await client.post(f"/api/v1/vendor/orders/{sub['id']}/ship", headers=v["h"], json={})
        ).json()
        consignment = shipment["consignment_id"]
        await _webhook(client, app, t, consignment, "delivered", notification_id=uuid.uuid4().hex)
    return h, order, sub, consignment


async def _scoped(app, tenant_id):
    """A tenant-scoped app-role session, the same one the API uses."""
    session = app.state.db.sessionmaker()
    return session


# ------------------------------------------------------------------------------ the invariants
async def test_a_prepaid_capture_books_the_whole_shipment_and_only_once(
    client, app, platform_headers
):
    t, staff, v = await _finance_shop(client, app, platform_headers, "led-alpha")
    h, order, sub, _ = await _pay_and_deliver(client, app, t, v, deliver=False)

    entries = (await client.get("/api/v1/admin/ledger", headers=staff)).json()
    by_account = {e["account"]: D(e["amount"]) for e in entries}
    assert by_account["gateway_clearing:bkash"] == D(sub["total"])  # the gateway owes the tenant
    assert by_account["vendor_payable"] == D("900.00")  # 1000 items less 10% commission
    assert by_account["tenant_commission_revenue"] == D("100.00")
    assert by_account["shipping_fee_revenue"] == D(sub["shipping_fee"])
    assert {e["direction"] for e in entries if e["account"] == "vendor_payable"} == {"credit"}

    balance = (await client.get("/api/v1/admin/ledger/trial-balance", headers=staff)).json()
    assert balance["balanced"] is True and D(balance["total_debits"]) > 0

    # posting the same capture again is a no-op, whatever calls it
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        order_id = await s.scalar(
            text("SELECT order_id FROM sub_orders WHERE tenant_id = :t AND id = :s"),
            {"t": t["id"], "s": sub["id"]},
        )
        assert await ledger.post_capture(s, t["id"], order_id) == []
    again = (await client.get("/api/v1/admin/ledger", headers=staff)).json()
    assert len(again) == len(entries)


async def test_the_database_refuses_an_unbalanced_group(client, app, platform_headers):
    t, staff, v = await _finance_shop(client, app, platform_headers, "led-beta")
    # the service refuses it first...
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        try:
            await ledger.post(
                s,
                t["id"],
                entry_type="adjustment",
                ref_type="test",
                ref_id=uuid.uuid4(),
                lines=[ledger.Line(acct.TENANT_BANK, acct.DEBIT, D("10"))],
            )
            raise AssertionError("an unbalanced group must be refused")
        except ledger.LedgerError:
            pass
    # ...and the database refuses it even if something bypasses the service
    import sqlalchemy.exc

    try:
        async with app.state.db.sessionmaker() as s, s.begin():
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
            await s.execute(
                text(
                    """INSERT INTO ledger_entries (tenant_id, group_id, account, direction, amount,
                           entry_type, ref_type, ref_id)
                       VALUES (:t, gen_random_uuid(), 'tenant_bank', 'debit', 10, 'adjustment',
                               'test', gen_random_uuid())"""
                ),
                {"t": t["id"]},
            )
        raise AssertionError("the constraint trigger must reject a half-entry")
    except sqlalchemy.exc.IntegrityError as exc:
        assert "does not balance" in str(exc)


async def test_commission_uses_the_snapshot_not_todays_rate(client, app, platform_headers):
    t, staff, v = await _finance_shop(client, app, platform_headers, "led-gamma")
    h, order, sub, _ = await _pay_and_deliver(client, app, t, v, deliver=False)
    # the tenant triples its commission after the sale
    await client.post(
        f"/api/v1/admin/vendors/{v['id']}/commission", headers=staff, json={"rate": "0.30"}
    )
    h2, order2, sub2, _ = await _pay_and_deliver(client, app, t, v, deliver=False)
    entries = (
        await client.get("/api/v1/admin/ledger?account=tenant_commission_revenue", headers=staff)
    ).json()
    amounts = sorted(D(e["amount"]) for e in entries)
    assert amounts == [D("100.00"), D("300.00")]  # the old order keeps its old rate


async def test_cod_books_the_courier_then_the_bank(client, app, platform_headers):
    t, staff, v = await _finance_shop(client, app, platform_headers, "led-delta")
    h, order, sub, consignment = await _pay_and_deliver(client, app, t, v, method="cod")
    entries = {
        e["account"]: D(e["amount"])
        for e in (await client.get("/api/v1/admin/ledger", headers=staff)).json()
    }
    assert entries["courier_cod_receivable:steadfast"] == D(sub["total"])
    assert entries["vendor_payable"] == D("900.00")
    assert await _account(client, staff, "courier_cod_receivable:steadfast") == D(sub["total"])

    csv = f"consignment_id,cod_amount,delivery_fee\n{consignment},{sub['total']},70\n".encode()
    r = await client.post(
        "/api/v1/admin/courier-settlements",
        headers=staff,
        data={"courier": "steadfast", "statement_ref": "SF-LED-1"},
        files={"file": ("s.csv", csv, "text/csv")},
    )
    assert r.json()["matched"] == 1
    # the courier has paid: the receivable clears, the bank gains the net, the fee is an expense
    assert await _account(client, staff, "courier_cod_receivable:steadfast") == D("0.00")
    assert await _account(client, staff, acct.TENANT_BANK) == D(sub["total"]) - D("70.00")
    assert await _account(client, staff, acct.COURIER_FEE) == D("70.00")
    assert (await client.get("/api/v1/admin/ledger/trial-balance", headers=staff)).json()[
        "balanced"
    ] is True


async def _account(client, staff, account: str) -> D:
    rows = (await client.get("/api/v1/admin/ledger/trial-balance", headers=staff)).json()[
        "accounts"
    ]
    for row in rows:
        if row["account"] == account:
            return D(row["balance"])
    return D("0.00")


async def test_a_refund_unwinds_payable_commission_and_vat(client, app, platform_headers):
    t, staff, v = await _finance_shop(
        client,
        app,
        platform_headers,
        "led-epsilon",
        vat_pricing="exclusive",
        default_vat_rate="0.05",
    )
    h, order, sub, _ = await _pay_and_deliver(client, app, t, v)
    async with app.state.platform_db.engine.begin() as conn:
        item_id = str(
            await conn.scalar(
                text("SELECT id FROM order_items WHERE tenant_id = :t AND sub_order_id = :s"),
                {"t": t["id"], "s": sub["id"]},
            )
        )
    payable_before = D(
        (await client.get("/api/v1/vendor/balance", headers=v["h"])).json()["payable"]
    )
    assert payable_before == D("900.00")
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
    refund = (await client.post(f"/api/v1/admin/returns/{ret['id']}/refund", headers=staff)).json()
    assert refund["status"] == "completed"

    # the buyer gets back what they paid, VAT included, and the vendor keeps none of it
    assert D(refund["amount"]) == D("1050.00")
    assert D((await client.get("/api/v1/vendor/balance", headers=v["h"])).json()["payable"]) == 0
    reversal = (
        await client.get("/api/v1/admin/ledger?entry_type=return_reversal", headers=staff)
    ).json()
    accounts = {e["account"]: (e["direction"], D(e["amount"])) for e in reversal}
    assert accounts["vendor_payable"][0] == "debit"
    assert accounts["tenant_commission_revenue"][0] == "debit"
    assert accounts["vat_output_payable"][0] == "debit"
    assert accounts["gateway_clearing:bkash"][0] == "credit"
    assert (await client.get("/api/v1/admin/ledger/trial-balance", headers=staff)).json()[
        "balanced"
    ] is True


# ---------------------------------------------------------------------------------- the payouts
async def _payout_ready(client, app, platform_headers, slug):
    t, staff, v = await _finance_shop(client, app, platform_headers, slug)
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tenant_settings SET reserve_mode = 'none', payout_min_amount = 100, "
                "tds_rate = 0.05, maker_checker = false WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    # the payout method itself is Phase 6 (re-auth + OTP + 72h hold); here it is simply present
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                """INSERT INTO vendor_payout_methods (tenant_id, vendor_id, method, account_name,
                       details_ciphertext, account_hash, last4, created_by)
                   VALUES (:t, :v, 'bkash', 'Shop Owner', 'v1:seed', :h, '5678', 'seed')"""
            ),
            {"t": t["id"], "v": v["id"], "h": f"hash-{v['id']}"},
        )
    return t, staff, v


async def test_a_payout_batch_pays_each_vendor_once(client, app, platform_headers):
    t, staff, v = await _payout_ready(client, app, platform_headers, "led-zeta")
    await _pay_and_deliver(client, app, t, v, qty=2)
    batch = (
        await client.post(
            "/api/v1/admin/payout-batches", headers=staff, json={"period_end": "2026-02-07"}
        )
    ).json()
    assert batch["line_count"] == 1
    assert D(batch["gross_total"]) == D("1800.00")  # two units, 10% commission
    assert D(batch["tds_total"]) == D("90.00") and D(batch["net_total"]) == D("1710.00")

    # a second batch for the same period is refused outright
    assert (
        await client.post(
            "/api/v1/admin/payout-batches", headers=staff, json={"period_end": "2026-02-07"}
        )
    ).status_code == 409

    detail = (await client.get(f"/api/v1/admin/payout-batches/{batch['id']}", headers=staff)).json()
    line = detail["lines"][0]
    assert line["method"] == "bkash" and line["account_last4"] == "5678"
    # nothing can be paid before the batch is approved
    assert (
        await client.post(
            f"/api/v1/admin/payout-lines/{line['id']}/paid",
            headers=staff,
            json={"reference": "TOO-EARLY"},
        )
    ).status_code == 409

    assert (
        await client.post(f"/api/v1/admin/payout-batches/{batch['id']}/approve", headers=staff)
    ).json()["status"] == "approved"
    export = await client.get(
        f"/api/v1/admin/payout-batches/{batch['id']}/export?method=bkash", headers=staff
    )
    assert export.headers["content-type"].startswith("text/csv")
    assert "5678" in export.text and "1710.00" in export.text
    assert "01712345678" not in export.text  # a full wallet number never leaves the vault

    payable_before = D(
        (await client.get("/api/v1/vendor/balance", headers=v["h"])).json()["payable"]
    )
    paid = await client.post(
        f"/api/v1/admin/payout-lines/{line['id']}/paid",
        headers=staff,
        json={"reference": "BKASH-DISB-5521"},
    )
    assert paid.json() == {"id": line["id"], "status": "paid", "already": False}
    # marking it again changes nothing at all
    assert (
        await client.post(
            f"/api/v1/admin/payout-lines/{line['id']}/paid",
            headers=staff,
            json={"reference": "BKASH-DISB-5521"},
        )
    ).json()["already"] is True

    assert payable_before == D("1800.00")
    assert D((await client.get("/api/v1/vendor/balance", headers=v["h"])).json()["payable"]) == 0
    assert await _account(client, staff, acct.TDS_PAYABLE) == D("-90.00")  # credit balance
    assert (await client.get(f"/api/v1/admin/payout-batches/{batch['id']}", headers=staff)).json()[
        "status"
    ] == "completed"
    vendor_view = (await client.get("/api/v1/vendor/payouts", headers=v["h"])).json()
    assert vendor_view[0]["reference"] == "BKASH-DISB-5521" and vendor_view[0]["status"] == "paid"
    assert D((await client.get("/api/v1/vendor/balance", headers=v["h"])).json()["payable"]) == 0


async def test_holds_reserves_and_minimums_keep_money_back(client, app, platform_headers):
    t, staff, v = await _payout_ready(client, app, platform_headers, "led-eta")
    await _pay_and_deliver(client, app, t, v)
    # a window reserve holds back everything delivered inside the window
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tenant_settings SET reserve_mode = 'window', reserve_days = 7 WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    balance = (await client.get("/api/v1/vendor/balance", headers=v["h"])).json()
    assert D(balance["payable"]) == D("900.00") and D(balance["available"]) == D("0.00")
    empty = (
        await client.post(
            "/api/v1/admin/payout-batches", headers=staff, json={"period_end": "2026-03-07"}
        )
    ).json()
    assert empty["line_count"] == 0
    assert {x["reason"] for x in empty["skipped"] if x["vendor"] == "Led-Eta"} == {"below_minimum"}

    # once the window has passed, the money is available
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE shipments SET delivered_at = now() - interval '30 days' WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
        # nothing rewrites the ledger, not even a test: the window runs from delivery
    assert D((await client.get("/api/v1/vendor/balance", headers=v["h"])).json()["available"]) == D(
        "900.00"
    )

    # ...unless the vendor is on hold, and then not a taka moves
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                """INSERT INTO payout_holds (tenant_id, vendor_id, reason, created_by)
                   VALUES (:t, :v, 'dispute', 'staff')"""
            ),
            {"t": t["id"], "v": v["id"]},
        )
    held = (await client.get("/api/v1/vendor/balance", headers=v["h"])).json()
    assert held["on_hold"] is True and D(held["available"]) == 0
    batch = (
        await client.post(
            "/api/v1/admin/payout-batches", headers=staff, json={"period_end": "2026-03-14"}
        )
    ).json()
    assert batch["line_count"] == 0
    assert {x["reason"] for x in batch["skipped"] if x["vendor"] == "Led-Eta"} == {"payout_hold"}


async def test_maker_checker_stops_one_person_paying_themselves(client, app, platform_headers):
    t, staff, v = await _payout_ready(client, app, platform_headers, "led-theta")
    await _pay_and_deliver(client, app, t, v)
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_settings SET maker_checker = true WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    # a second finance user exists, so the preparer may not approve their own batch
    invited = await client.post(
        "/api/v1/admin/staff",
        headers=staff,
        json={"email": "finance2@example.com", "role": "finance"},
    )
    assert invited.status_code in (200, 201), invited.text
    batch = (
        await client.post(
            "/api/v1/admin/payout-batches", headers=staff, json={"period_end": "2026-04-04"}
        )
    ).json()
    assert batch["line_count"] == 1
    blocked = await client.post(
        f"/api/v1/admin/payout-batches/{batch['id']}/approve", headers=staff
    )
    assert blocked.status_code == 409 and blocked.json()["title"] == "maker_checker"

    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE staff_members SET status = 'active' WHERE tenant_id = :t"), {"t": t["id"]}
        )
    await set_password(app, "finance2@example.com", t["id"])
    token = await login(client, t["primary_host"], "finance2@example.com", "staff")
    client.cookies.clear()
    other = {"authorization": f"Bearer {token}", "host": t["primary_host"]}
    assert (
        await client.post(f"/api/v1/admin/payout-batches/{batch['id']}/approve", headers=other)
    ).json()["status"] == "approved"


# ----------------------------------------------------------------------------- the tax documents
async def test_tax_invoice_is_numbered_gapless_and_private(client, app, platform_headers):
    t, staff, v = await _finance_shop(
        client, app, platform_headers, "led-iota", vat_pricing="exclusive", default_vat_rate="0.05"
    )
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_settings SET vat_bin = '004561234-0101' WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    h, order, sub, _ = await _pay_and_deliver(client, app, t, v)
    invoices = (await client.get(f"/api/v1/me/orders/{order['number']}/invoices", headers=h)).json()
    assert len(invoices) == 1
    number = invoices[0]["number"]
    assert number.startswith("INV-") and invoices[0]["url"].startswith("/internal/private/")
    # asking again returns the same document, never a second number
    assert (await client.get(f"/api/v1/me/orders/{order['number']}/invoices", headers=h)).json()[0][
        "number"
    ] == number

    pdf = await client.get(invoices[0]["url"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    listed = (await client.get("/api/v1/admin/tax-documents", headers=staff)).json()
    assert listed[0]["number"] == number and listed[0]["seller_bin"] == "004561234-0101"
    order_detail = (await client.get(f"/api/v1/me/orders/{order['number']}", headers=h)).json()
    assert D(listed[0]["vat_amount"]) == D(order_detail["vat_total"]) > 0

    # a second buyer's invoice takes the next number in the same series
    h2, order2, sub2, _ = await _pay_and_deliver(client, app, t, v)
    second = (
        await client.get(f"/api/v1/me/orders/{order2['number']}/invoices", headers=h2)
    ).json()[0]
    assert int(second["number"].rsplit("-", 1)[1]) == int(number.rsplit("-", 1)[1]) + 1

    # another buyer cannot read someone else's invoice list
    assert (
        await client.get(f"/api/v1/me/orders/{order['number']}/invoices", headers=h2)
    ).status_code == 404


async def test_a_credit_note_documents_the_refund(client, app, platform_headers):
    t, staff, v = await _finance_shop(
        client, app, platform_headers, "led-kappa", vat_pricing="exclusive", default_vat_rate="0.05"
    )
    h, order, sub, _ = await _pay_and_deliver(client, app, t, v)
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        doc = await documents.issue(
            s,
            app.state.private_storage,
            t["id"],
            sub_order_id=sub["id"],
            kind="credit_note",
            amount=D("500.00"),
            vat_amount=D("25.00"),
        )
    assert doc["number"].startswith("CRN-") and D(doc["taxable_amount"]) == D("475.00")
    stored = await app.state.private_storage.head(doc["document_key"])
    assert stored and stored > 500  # a real PDF, in the private bucket
    kinds = {
        d["kind"] for d in (await client.get("/api/v1/admin/tax-documents", headers=staff)).json()
    }
    assert kinds == {"credit_note"}


async def test_vendor_sees_only_its_own_books(client, app, platform_headers):
    t, staff, cat = await _market(client, app, platform_headers)
    await _gateway(app)
    await _courier(app)
    await _configure(client, staff)
    await _configure_courier(client, staff)
    a = await _vendor(client, app, t, staff, "led-mu-a", cat, price="1000", stock=5)
    b = await _vendor(client, app, t, staff, "led-mu-b", cat, price="1000", stock=5)
    await _pay_and_deliver(client, app, t, a, deliver=False)
    a_entries = (await client.get("/api/v1/vendor/ledger", headers=a["h"])).json()
    b_entries = (await client.get("/api/v1/vendor/ledger", headers=b["h"])).json()
    assert a_entries and all(e["vendor_id"] == a["id"] for e in a_entries)
    assert b_entries == []
    assert D((await client.get("/api/v1/vendor/balance", headers=b["h"])).json()["payable"]) == 0


def test_payout_period_end_follows_the_schedule():
    # Saturday closes the week in Bangladesh
    assert payouts.period_end_for("weekly", date(2026, 9, 17)) == date(2026, 9, 12)
    assert payouts.period_end_for("weekly", date(2026, 9, 12)) == date(2026, 9, 5)
    assert payouts.period_end_for("monthly", date(2026, 9, 17)) == date(2026, 8, 31)


async def test_reconciliation_reports_drift_without_fixing_it(client, app, platform_headers):
    t, staff, v = await _finance_shop(client, app, platform_headers, "led-nu")
    h, order, sub, consignment = await _pay_and_deliver(client, app, t, v, method="cod")
    clean = (await client.get("/api/v1/admin/ledger/reconciliation", headers=staff)).json()
    assert clean["clean"] is True and clean["drift_count"] == 0
    named = {c["check"] for c in clean["checks"]}
    assert "trial_balance" in named and "courier_cod_receivable:steadfast" in named

    # someone settles a receivable outside the ledger: the books and the world disagree
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE cod_receivables SET status = 'settled' WHERE tenant_id = :t"),
            {"t": t["id"]},
        )
    drifted = (await client.get("/api/v1/admin/ledger/reconciliation", headers=staff)).json()
    assert drifted["clean"] is False
    cod = next(c for c in drifted["checks"] if c["check"] == "courier_cod_receivable:steadfast")
    assert D(cod["ledger"]) > 0 and D(cod["expected"]) == 0 and D(cod["drift"]) == D(cod["ledger"])
    # and the ledger is untouched: reconciliation reports, it does not adjust
    assert (
        D(
            (await client.get("/api/v1/admin/ledger/trial-balance", headers=staff)).json()[
                "total_debits"
            ]
        )
        > 0
    )
