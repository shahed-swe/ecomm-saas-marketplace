import uuid

import pyotp
from sqlalchemy import text

from app.core.passwords import hash_password
from app.tests.conftest import PASSWORD, login, make_tenant, set_password


async def _tenant(client, app, platform_headers, mode="single"):
    return await make_tenant(
        client,
        app,
        platform_headers,
        slug=f"auth{uuid.uuid4().hex[:6]}",
        name="Auth shop",
        store_mode=mode,
    )


async def test_register_login_me_refresh_rotation_and_reuse_detection(
    client, app, platform_headers
):
    t = await _tenant(client, app, platform_headers)
    host = {"host": t["primary_host"]}
    r = await client.post(
        "/api/v1/auth/register",
        headers=host,
        json={"email": "Buyer@Example.com", "password": PASSWORD, "full_name": "B"},
    )
    assert r.status_code == 201, r.text
    first_rt = r.cookies.get("rt")
    assert first_rt and r.json()["refresh_token"] is None  # web: cookie only

    access = await login(client, t["primary_host"], "buyer@example.com", "buyer")
    me = await client.get("/api/v1/me", headers={**host, "authorization": f"Bearer {access}"})
    assert me.status_code == 200 and me.json()["kind"] == "buyer"

    client.cookies.clear()
    r1 = await client.post("/api/v1/auth/refresh", headers=host, json={"refresh_token": first_rt})
    assert r1.status_code == 200
    second_rt = r1.cookies.get("rt")
    assert second_rt and second_rt != first_rt

    # replaying the first (rotated) token revokes the whole family — and that revocation is committed
    client.cookies.clear()
    replay = await client.post(
        "/api/v1/auth/refresh", headers=host, json={"refresh_token": first_rt}
    )
    assert replay.status_code == 401
    after = await client.post(
        "/api/v1/auth/refresh", headers=host, json={"refresh_token": second_rt}
    )
    assert after.status_code == 401
    client.cookies.clear()


async def test_app_clients_get_refresh_token_in_body_and_logout_revokes(
    client, app, platform_headers
):
    t = await _tenant(client, app, platform_headers)
    h = {"host": t["primary_host"], "x-client": "app"}
    r = await client.post(
        "/api/v1/auth/login",
        headers=h,
        json={"email": t["owner_email"], "password": PASSWORD, "surface": "buyer"},
    )
    rt = r.json()["refresh_token"]
    assert rt
    assert (
        await client.post("/api/v1/auth/logout", headers=h, json={"refresh_token": rt})
    ).status_code == 204
    assert (
        await client.post("/api/v1/auth/refresh", headers=h, json={"refresh_token": rt})
    ).status_code == 401


async def test_wrong_password_and_wrong_surface_are_generic_401(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    h = {"host": t["primary_host"]}
    await client.post(
        "/api/v1/auth/register", headers=h, json={"email": "b2@example.com", "password": PASSWORD}
    )
    bad = await client.post(
        "/api/v1/auth/login",
        headers=h,
        json={"email": "b2@example.com", "password": "nope-nope-nope", "surface": "buyer"},
    )
    staff = await client.post(
        "/api/v1/auth/login",
        headers=h,
        json={"email": "b2@example.com", "password": PASSWORD, "surface": "staff"},
    )
    ghost = await client.post(
        "/api/v1/auth/login",
        headers=h,
        json={"email": "ghost@example.com", "password": PASSWORD, "surface": "buyer"},
    )
    assert bad.status_code == staff.status_code == ghost.status_code == 401
    assert bad.json()["detail"] == staff.json()["detail"] == ghost.json()["detail"]
    client.cookies.clear()


async def test_accounts_are_per_tenant(client, app, platform_headers):
    t1 = await _tenant(client, app, platform_headers)
    t2 = await _tenant(client, app, platform_headers)
    body = {"email": "same@example.com", "password": PASSWORD}
    assert (
        await client.post("/api/v1/auth/register", headers={"host": t1["primary_host"]}, json=body)
    ).status_code == 201
    assert (
        await client.post("/api/v1/auth/register", headers={"host": t2["primary_host"]}, json=body)
    ).status_code == 201
    tok = await login(client, t1["primary_host"], "same@example.com", "buyer")
    assert (
        await client.get(
            "/api/v1/me", headers={"host": t2["primary_host"], "authorization": f"Bearer {tok}"}
        )
    ).status_code == 401
    client.cookies.clear()


async def test_otp_login_flow_attempt_limit_and_throttle(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    h = {"host": t["primary_host"]}
    sms = app.state.sms
    assert (
        await client.post("/api/v1/auth/otp/request", headers=h, json={"phone": "12345"})
    ).status_code == 422
    r = await client.post("/api/v1/auth/otp/request", headers=h, json={"phone": "017 1234-5678"})
    assert r.status_code == 202
    code = sms.last_code("+8801712345678")
    sent_before = len(sms.sent)
    # resend inside the throttle window: accepted but nothing sent
    assert (
        await client.post("/api/v1/auth/otp/request", headers=h, json={"phone": "01712345678"})
    ).status_code == 202
    assert len(sms.sent) == sent_before

    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        r = await client.post(
            "/api/v1/auth/otp/verify", headers=h, json={"phone": "01712345678", "code": wrong}
        )
        assert r.status_code == 400
    # attempts were persisted despite the 400s: the right code is now refused too
    r = await client.post(
        "/api/v1/auth/otp/verify", headers=h, json={"phone": "01712345678", "code": code}
    )
    assert r.status_code == 400

    # fresh challenge after the throttle window
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE otp_challenges SET created_at = now() - interval '2 minutes' "
                "WHERE tenant_id = :t"
            ),
            {"t": t["id"]},
        )
    await client.post("/api/v1/auth/otp/request", headers=h, json={"phone": "+8801712345678"})
    code = sms.last_code("+8801712345678")
    r = await client.post(
        "/api/v1/auth/otp/verify", headers=h, json={"phone": "01712345678", "code": code}
    )
    assert r.status_code == 200 and r.json()["kind"] == "buyer"
    # single use
    r = await client.post(
        "/api/v1/auth/otp/verify", headers=h, json={"phone": "01712345678", "code": code}
    )
    assert r.status_code == 400
    async with app.state.platform_db.engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM users WHERE tenant_id=:t AND phone='+8801712345678'"),
                {"t": t["id"]},
            )
        ).scalar()
    assert n == 1
    client.cookies.clear()


async def test_otp_rate_limit(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    h = {"host": t["primary_host"]}
    codes = [
        (
            await client.post("/api/v1/auth/otp/request", headers=h, json={"phone": "01812345678"})
        ).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [202] * 5 and codes[5] == 429


async def test_staff_roles_permissions_audit_and_immediate_revocation(
    client, app, platform_headers
):
    t = await _tenant(client, app, platform_headers)
    owner = {"authorization": f"Bearer {t['staff_token']}", "host": t["primary_host"]}
    roles = {r["key"] for r in (await client.get("/api/v1/admin/roles", headers=owner)).json()}
    assert {"owner", "finance", "support", "moderator", "content"} <= roles

    r = await client.post(
        "/api/v1/admin/staff",
        headers=owner,
        json={"email": "support@example.com", "role": "support"},
    )
    assert r.status_code == 201, r.text
    member_id = r.json()["id"]
    assert (
        await client.post(
            "/api/v1/admin/staff", headers=owner, json={"email": "x@example.com", "role": "owner"}
        )
    ).status_code == 409

    await set_password(app, "support@example.com", t["id"])
    sup_tok = await login(client, t["primary_host"], "support@example.com", "staff")
    sup = {"authorization": f"Bearer {sup_tok}", "host": t["primary_host"]}
    assert (
        await client.get("/api/v1/admin/vendors", headers=sup)
    ).status_code == 200  # vendors.read
    assert (await client.get("/api/v1/admin/staff", headers=sup)).status_code == 403  # staff.manage
    assert (await client.get("/api/v1/admin/audit", headers=sup)).status_code == 403  # audit.read

    log = (
        await client.get("/api/v1/admin/audit", headers=owner, params={"entity": "staff"})
    ).json()
    assert log[0]["action"] == "staff.invite" and log[0]["entity_id"] == member_id

    r = await client.patch(
        f"/api/v1/admin/staff/{member_id}", headers=owner, json={"status": "disabled"}
    )
    assert r.status_code == 200
    # access token still unexpired, but permissions come from the DB: access is gone now
    assert (await client.get("/api/v1/admin/vendors", headers=sup)).status_code == 401
    client.cookies.clear()


async def test_audit_log_is_append_only(app, world, settings):
    """Two defences: runtime roles have no UPDATE grant, and even the owner hits the trigger."""
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.tests.conftest import ADMIN_URL

    async with app.state.platform_db.engine.connect() as conn:
        try:
            async with conn.begin():
                await conn.execute(text("UPDATE audit_log SET action='tampered'"))
            raise AssertionError("update allowed")
        except DBAPIError as exc:
            assert "permission denied" in str(exc)
    owner = create_async_engine(
        ADMIN_URL.rsplit("/", 1)[0] + "/" + settings.database_url.rsplit("/", 1)[1]
    )
    async with owner.connect() as conn:
        try:
            async with conn.begin():
                await conn.execute(text("UPDATE audit_log SET action='tampered'"))
            raise AssertionError("update allowed")
        except DBAPIError as exc:
            assert "append-only" in str(exc)
    await owner.dispose()


async def test_vendor_roles(client, app, world):
    A = world["A"]
    owner = A.vendor_actors["A1"].headers()
    r = await client.post(
        "/api/v1/vendor/staff", headers=owner, json={"email": "packer@example.com", "role": "staff"}
    )
    assert r.status_code == 201, r.text
    vu_id = r.json()["id"]
    await set_password(app, "packer@example.com", A.id)
    tok = await login(client, A.host, "packer@example.com", "vendor", A.vendors["A1"])
    staff = {"authorization": f"Bearer {tok}", "host": A.host}
    assert (await client.get("/api/v1/vendor/storefront", headers=staff)).status_code == 200
    assert (await client.get("/api/v1/vendor/staff", headers=staff)).status_code == 403
    sf = A.storefronts["A1"]
    assert (
        await client.patch(f"/api/v1/vendor/storefronts/{sf}", headers=staff, json={"tagline": "x"})
    ).status_code == 403
    # a login for a vendor the user does not belong to fails generically
    r = await client.post(
        "/api/v1/auth/login",
        headers={"host": A.host},
        json={
            "email": "packer@example.com",
            "password": PASSWORD,
            "surface": "vendor",
            "vendor_id": A.vendors["A2"],
        },
    )
    assert r.status_code == 401
    # removal takes effect immediately
    assert (
        await client.patch(
            f"/api/v1/vendor/staff/{vu_id}", headers=owner, json={"status": "disabled"}
        )
    ).status_code == 200
    assert (await client.get("/api/v1/vendor/storefront", headers=staff)).status_code == 401
    client.cookies.clear()


async def test_platform_login_requires_totp(client, app):
    secret = pyotp.random_base32()
    email = f"root{uuid.uuid4().hex[:6]}@example.com"
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO platform_users (email, password_hash, totp_secret) VALUES (:e,:p,:s)"
            ),
            {"e": email, "p": hash_password(PASSWORD), "s": secret},
        )
    bad = await client.post(
        "/platform/v1/auth/login", json={"email": email, "password": PASSWORD, "totp": "000000"}
    )
    assert bad.status_code == 401
    ok = await client.post(
        "/platform/v1/auth/login",
        json={"email": email, "password": PASSWORD, "totp": pyotp.TOTP(secret).now()},
    )
    assert ok.status_code == 200
    tok = ok.json()["access_token"]
    assert (
        await client.get("/platform/v1/tenants", headers={"authorization": f"Bearer {tok}"})
    ).status_code == 200


async def test_app_role_cannot_read_platform_users(settings):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(settings.database_url)
    try:
        async with eng.connect() as conn:
            try:
                await conn.execute(text("SELECT * FROM platform_users"))
                raise AssertionError("app role read platform_users")
            except DBAPIError as exc:
                assert "permission denied" in str(exc)
    finally:
        await eng.dispose()
