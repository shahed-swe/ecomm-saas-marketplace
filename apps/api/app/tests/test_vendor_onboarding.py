import uuid

from sqlalchemy import text

from app.tests.conftest import PASSWORD, login, make_tenant, set_password


def _staff(t):
    return {"authorization": f"Bearer {t['staff_token']}", "host": t["primary_host"]}


async def _market(client, app, platform_headers, signup="open"):
    t = await make_tenant(
        client,
        app,
        platform_headers,
        slug=f"mk{uuid.uuid4().hex[:6]}",
        name="Market",
        store_mode="multi",
    )
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_settings SET vendor_signup = :s WHERE tenant_id = :t"),
            {"s": signup, "t": t["id"]},
        )
    return t


def _signup(**kw):
    body = {
        "display_name": "Rahim Traders",
        "slug": f"rahim{uuid.uuid4().hex[:5]}",
        "legal_name": "Rahim Traders",
        "business_type": "proprietorship",
        "contact_phone": "01711000111",
        "district": "Dhaka",
        "email": f"rahim{uuid.uuid4().hex[:5]}@example.com",
        "password": PASSWORD,
        "accept_agreement": True,
    }
    body.update(kw)
    return body


async def _upload(client, vh, doc_type, number=None):
    up = (
        await client.post(
            "/api/v1/vendor/documents/upload-url",
            headers=vh,
            json={"doc_type": doc_type, "content_type": "image/jpeg", "byte_size": 20},
        )
    ).json()
    assert (
        await client.put(up["url"], content=b"\xff\xd8\xff fake jpeg", headers=up["headers"])
    ).status_code == 201
    r = await client.post(
        "/api/v1/vendor/documents",
        headers=vh,
        json={"doc_type": doc_type, "storage_key": up["storage_key"], "document_number": number},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"], up["storage_key"]


async def test_signup_modes(client, app, platform_headers):
    closed = await _market(client, app, platform_headers, "closed")
    assert (
        await client.post(
            "/api/v1/vendor-signup", headers={"host": closed["primary_host"]}, json=_signup()
        )
    ).status_code == 403
    inv = await _market(client, app, platform_headers, "invite_only")
    h = {"host": inv["primary_host"]}
    assert (
        await client.post("/api/v1/vendor-signup", headers=h, json=_signup())
    ).status_code == 403
    code = (
        await client.post(
            "/api/v1/admin/vendor-invites",
            headers=_staff(inv),
            json={"tier": "launch", "commission_rate": "0", "max_uses": 1},
        )
    ).json()["code"]
    r = await client.post(
        "/api/v1/vendor-signup", headers=h, json=_signup(invite_code=code.lower())
    )
    assert r.status_code == 201, r.text
    assert (
        await client.post("/api/v1/vendor-signup", headers=h, json=_signup(invite_code=code))
    ).status_code == 409
    async with app.state.platform_db.engine.connect() as conn:
        rule = (
            await conn.execute(
                text("SELECT rate FROM commission_rules WHERE scope_id = :v"),
                {"v": r.json()["vendor_id"]},
            )
        ).scalar()
        tier = (
            await conn.execute(
                text("SELECT tier FROM vendors WHERE id = :v"), {"v": r.json()["vendor_id"]}
            )
        ).scalar()
    assert rule == 0 and tier == "launch"
    single = await make_tenant(
        client, app, platform_headers, slug=f"sg{uuid.uuid4().hex[:6]}", name="Single"
    )
    assert (
        await client.post(
            "/api/v1/vendor-signup", headers={"host": single["primary_host"]}, json=_signup()
        )
    ).status_code == 403
    assert (
        await client.post("/api/v1/vendor-signup", headers=h, json=_signup(accept_agreement=False))
    ).status_code == 422
    client.cookies.clear()


async def test_full_onboarding_to_approval(client, app, platform_headers):
    t = await _market(client, app, platform_headers)
    host = t["primary_host"]
    r = await client.post("/api/v1/vendor-signup", headers={"host": host}, json=_signup())
    vid = r.json()["vendor_id"]
    vh = {"authorization": f"Bearer {r.json()['access_token']}", "host": host}

    state = (await client.get("/api/v1/vendor/onboarding", headers=vh)).json()
    assert state["status"] == "registered" and set(state["missing_documents"]) == {
        "trade_licence",
        "nid",
        "bank_proof",
    }
    assert (await client.post("/api/v1/vendor/onboarding/submit", headers=vh)).status_code == 409

    bad = await client.post(
        "/api/v1/vendor/documents/upload-url",
        headers=vh,
        json={"doc_type": "nid", "content_type": "text/html", "byte_size": 10},
    )
    assert bad.status_code == 422
    docs = {}
    for dt, num in (
        ("trade_licence", "TRAD-2024-99"),
        ("nid", "19901234567890"),
        ("bank_proof", None),
    ):
        docs[dt], _ = await _upload(client, vh, dt, num)
    state = (await client.post("/api/v1/vendor/onboarding/submit", headers=vh)).json()
    assert state["status"] == "under_review" and state["can_submit"] is False

    # the vendor cannot approve itself, and staff cannot approve before documents are approved
    staff = _staff(t)
    assert (
        await client.post(
            f"/api/v1/admin/vendors/{vid}/transition", headers=staff, json={"to": "approved"}
        )
    ).status_code == 409
    rev = (await client.get(f"/api/v1/admin/vendors/{vid}/review", headers=staff)).json()
    assert [e["to_status"] for e in rev["events"]] == ["documents_submitted", "under_review"]
    nid = next(d for d in rev["documents"] if d["doc_type"] == "nid")
    assert nid["number_last4"] == "7890"
    file = await client.get(nid["view_url"])
    assert file.status_code == 200 and file.content.startswith(b"\xff\xd8\xff")
    assert file.headers["cache-control"] == "private, no-store"

    assert (
        await client.patch(
            f"/api/v1/admin/vendor-documents/{docs['nid']}",
            headers=staff,
            json={"status": "rejected"},
        )
    ).status_code == 422
    for d in docs.values():
        assert (
            await client.patch(
                f"/api/v1/admin/vendor-documents/{d}", headers=staff, json={"status": "approved"}
            )
        ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/admin/vendors/{vid}/transition", headers=staff, json={"to": "suspended"}
        )
    ).status_code == 422  # reason required
    r = await client.post(
        f"/api/v1/admin/vendors/{vid}/transition", headers=staff, json={"to": "approved"}
    )
    assert r.status_code == 200 and r.json()["from"] == "under_review"

    # suspension: vendor can read, cannot write
    await client.post(
        f"/api/v1/admin/vendors/{vid}/transition",
        headers=staff,
        json={"to": "suspended", "reason": "late shipments"},
    )
    assert (await client.get("/api/v1/vendor/onboarding", headers=vh)).status_code == 200
    assert (
        await client.patch("/api/v1/vendor/profile", headers=vh, json={"district": "Khulna"})
    ).status_code == 423
    assert (
        await client.post(
            f"/api/v1/admin/vendors/{vid}/transition", headers=staff, json={"to": "approved"}
        )
    ).status_code == 200
    client.cookies.clear()


async def test_document_keys_cannot_be_borrowed_and_duplicates_are_flagged(
    client, app, platform_headers, world
):
    t = await _market(client, app, platform_headers)
    host = t["primary_host"]
    v1 = await client.post("/api/v1/vendor-signup", headers={"host": host}, json=_signup())
    v2 = await client.post(
        "/api/v1/vendor-signup", headers={"host": host}, json=_signup(contact_phone="01711000111")
    )
    h1 = {"authorization": f"Bearer {v1.json()['access_token']}", "host": host}
    h2 = {"authorization": f"Bearer {v2.json()['access_token']}", "host": host}
    _, key1 = await _upload(client, h1, "nid", "1990 1234 5678 90")
    # vendor 2 tries to register vendor 1's uploaded file
    r = await client.post(
        "/api/v1/vendor/documents", headers=h2, json={"doc_type": "nid", "storage_key": key1}
    )
    assert r.status_code == 404
    await _upload(client, h2, "nid", "19901234567890")
    rev = (
        await client.get(
            f"/api/v1/admin/vendors/{v2.json()['vendor_id']}/review", headers=_staff(t)
        )
    ).json()
    assert "document number matches another vendor" in rev["duplicate_signals"]
    assert any("phone shared" in s for s in rev["duplicate_signals"])
    # the same NID number in another tenant is not a signal here (hash is tenant-keyed)
    assert (
        await client.post(
            "/api/v1/vendor/documents",
            headers=world["A"].vendor_actors["A1"].headers(),
            json={"doc_type": "nid", "storage_key": key1},
        )
    ).status_code == 404
    client.cookies.clear()


async def test_payout_method_requires_reauth_holds_and_encrypts(client, app, platform_headers):
    t = await _market(client, app, platform_headers)
    host = t["primary_host"]
    r = await client.post(
        "/api/v1/vendor-signup", headers={"host": host}, json=_signup(contact_phone="01811222333")
    )
    vid = r.json()["vendor_id"]
    vh = {"authorization": f"Bearer {r.json()['access_token']}", "host": host}
    bank = {
        "method": "bank",
        "account_name": "Rahim Traders",
        "account_number": "1234 5678 9012",
        "bank_name": "Dutch-Bangla Bank",
        "branch_name": "Motijheel",
        "routing_number": "090271234",
    }

    assert (
        await client.put("/api/v1/vendor/payout-method", headers=vh, json=bank)
    ).status_code == 401
    assert (await client.post("/api/v1/vendor/reauth/request", headers=vh)).status_code == 202
    code = app.state.sms.last_code("+8801811222333")
    assert (
        await client.post(
            "/api/v1/vendor/reauth/verify",
            headers=vh,
            json={"code": "000001" if code != "000001" else "000002"},
        )
    ).status_code == 400
    tok = (
        await client.post("/api/v1/vendor/reauth/verify", headers=vh, json={"code": code})
    ).json()["reauth_token"]

    first = await client.put(
        "/api/v1/vendor/payout-method", headers={**vh, "x-reauth-token": tok}, json=bank
    )
    assert (
        first.status_code == 200
        and first.json()["last4"] == "9012"
        and first.json()["on_hold_until"] is None
    )
    assert "1234" not in first.text
    # re-auth token is single use
    assert (
        await client.put(
            "/api/v1/vendor/payout-method", headers={**vh, "x-reauth-token": tok}, json=bank
        )
    ).status_code == 401

    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE otp_challenges SET created_at = now() - interval '2 minutes' WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    await client.post("/api/v1/vendor/reauth/request", headers=vh)
    tok2 = (
        await client.post(
            "/api/v1/vendor/reauth/verify",
            headers=vh,
            json={"code": app.state.sms.last_code("+8801811222333")},
        )
    ).json()["reauth_token"]
    sent_before = len(app.state.sms.sent)
    changed = await client.put(
        "/api/v1/vendor/payout-method",
        headers={**vh, "x-reauth-token": tok2},
        json={"method": "bkash", "account_name": "Rahim", "bkash_number": "01999888777"},
    )
    assert changed.status_code == 200 and changed.json()["on_hold_until"] is not None
    assert len(app.state.sms.sent) > sent_before and "held" in app.state.sms.sent[-1][2]

    async with app.state.platform_db.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT status, details_ciphertext FROM vendor_payout_methods WHERE vendor_id = :v"
                ),
                {"v": vid},
            )
        ).all()
    assert sorted(r.status for r in rows) == ["active", "replaced"]
    assert all(
        "01999888777" not in r.details_ciphertext and "123456789012" not in r.details_ciphertext
        for r in rows
    )
    from app.core.crypto import Encryptor

    active = next(r for r in rows if r.status == "active")
    assert Encryptor(app.state.settings.data_encryption_key).decrypt(
        active.details_ciphertext, context=f"payout:{t['id']}:{vid}"
    ) == {"bkash_number": "+8801999888777"}

    # a manager cannot touch payout details
    await client.post(
        "/api/v1/vendor/staff", headers=vh, json={"email": "mgr@example.com", "role": "manager"}
    )
    await set_password(app, "mgr@example.com", t["id"])
    mh = {
        "authorization": f"Bearer {await login(client, host, 'mgr@example.com', 'vendor', vid)}",
        "host": host,
    }
    assert (await client.post("/api/v1/vendor/reauth/request", headers=mh)).status_code == 403
    assert (await client.get("/api/v1/vendor/payout-method", headers=mh)).status_code == 403
    client.cookies.clear()


async def test_vendor_events_are_append_only(app, settings, world):
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.tests.conftest import ADMIN_URL

    owner = create_async_engine(
        ADMIN_URL.rsplit("/", 1)[0] + "/" + settings.database_url.rsplit("/", 1)[1]
    )
    try:
        async with owner.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO vendor_events (tenant_id, vendor_id, from_status, to_status, actor_kind, actor_id) "
                    "VALUES (:t, :v, 'a', 'b', 'test', 'test')"
                ),
                {"t": world["A"].id, "v": world["A"].vendors["A1"]},
            )
            await conn.commit()
            try:
                async with conn.begin():
                    await conn.execute(text("DELETE FROM vendor_events"))
                raise AssertionError("vendor_events mutable")
            except Exception as exc:  # noqa: BLE001
                assert "append-only" in str(exc)
    finally:
        await owner.dispose()
