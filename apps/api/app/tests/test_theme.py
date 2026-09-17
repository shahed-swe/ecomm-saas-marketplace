import copy
import io
import uuid

from PIL import Image
from sqlalchemy import text

from app.modules.theme.contrast import check_tokens, ratio
from app.modules.theme.presets import PRESETS, preset_document
from app.tests.conftest import login, make_tenant, set_password


def _h(t):
    return {"authorization": f"Bearer {t['staff_token']}", "host": t["primary_host"]}


async def _tenant(client, app, platform_headers):
    return await make_tenant(
        client, app, platform_headers, slug=f"th{uuid.uuid4().hex[:6]}", name="Theme shop"
    )


def _png(w=800, h=400, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_every_preset_passes_contrast():
    for key in PRESETS:
        assert check_tokens(preset_document(key).tokens) == [], key


def test_contrast_ratio_reference_values():
    assert ratio("0 0 0", "255 255 255") == 21.0
    assert ratio("255 255 255", "255 255 255") == 1.0


async def test_new_tenant_has_published_default_theme(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    r = await client.get("/api/v1/theme", headers={"host": t["primary_host"]})
    assert r.status_code == 200
    assert r.json()["version"] == 1 and r.json()["document"]["preset"] == "minimal"
    etag = r.headers["etag"]
    r2 = await client.get(
        "/api/v1/theme", headers={"host": t["primary_host"], "if-none-match": etag}
    )
    assert r2.status_code == 304


async def test_draft_preview_publish_rollback(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    h = _h(t)
    host = {"host": t["primary_host"]}
    r = await client.post("/api/v1/admin/theme/preset", headers=h, json={"preset": "bazaar"})
    assert r.status_code == 200 and r.json()["preset"] == "bazaar"

    # public still sees v1 (draft is private)
    assert (await client.get("/api/v1/theme", headers=host)).json()["document"][
        "preset"
    ] == "minimal"
    admin = (await client.get("/api/v1/admin/theme", headers=h)).json()
    assert admin["unpublished_changes"] is True

    tok = (await client.post("/api/v1/admin/theme/preview-token", headers=h)).json()["token"]
    prev = await client.get("/api/v1/theme", headers=host, params={"preview": tok})
    assert prev.json()["preview"] is True and prev.json()["document"]["preset"] == "bazaar"
    assert prev.headers["cache-control"] == "no-store"

    pub = await client.post("/api/v1/admin/theme/publish", headers=h, json={"note": "eid look"})
    assert pub.status_code == 200 and pub.json()["number"] == 2
    assert (await client.get("/api/v1/theme", headers=host)).json()["document"][
        "preset"
    ] == "bazaar"

    versions = (await client.get("/api/v1/admin/theme/versions", headers=h)).json()
    v1 = next(v for v in versions if v["number"] == 1)
    r = await client.post(f"/api/v1/admin/theme/versions/{v1['id']}/restore", headers=h)
    assert r.status_code == 200
    assert (await client.get("/api/v1/theme", headers=host)).json()["document"][
        "preset"
    ] == "minimal"
    log = (
        await client.get("/api/v1/admin/audit", headers=h, params={"entity": "theme_version"})
    ).json()
    assert [e["action"] for e in log[:2]] == ["theme.rollback", "theme.publish"]


async def test_preview_token_is_bound_to_its_tenant(client, app, platform_headers):
    t1 = await _tenant(client, app, platform_headers)
    t2 = await _tenant(client, app, platform_headers)
    await client.post("/api/v1/admin/theme/preset", headers=_h(t1), json={"preset": "fashion"})
    tok = (await client.post("/api/v1/admin/theme/preview-token", headers=_h(t1))).json()["token"]
    r = await client.get(
        "/api/v1/theme", headers={"host": t2["primary_host"]}, params={"preview": tok}
    )
    assert r.json()["preview"] is False


async def test_low_contrast_cannot_be_published(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    h = _h(t)
    doc = preset_document("minimal").model_dump(mode="json", by_alias=True)
    bad = copy.deepcopy(doc)
    bad["tokens"]["colors"]["fg"] = "250 250 250"  # near-white text on white
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=bad)).status_code == 200
    r = await client.post("/api/v1/admin/theme/publish", headers=h, json={})
    assert r.status_code == 422 and "fg on bg" in r.json()["detail"]
    issues = (await client.get("/api/v1/admin/theme", headers=h)).json()["contrast_issues"]
    assert issues


async def test_theme_data_cannot_inject(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    h = _h(t)
    doc = preset_document("minimal").model_dump(mode="json", by_alias=True)
    cases = []
    x = copy.deepcopy(doc)
    x["tokens"]["colors"]["primary"] = "1 2 3;}</style><script>"
    cases.append(x)
    x = copy.deepcopy(doc)
    x["tokens"]["radius"] = "1px;}body{display:none"
    cases.append(x)
    x = copy.deepcopy(doc)
    x["tokens"]["font_body"] = "https://evil.example/font.css"
    cases.append(x)
    x = copy.deepcopy(doc)
    x["brand"]["logo_url"] = "https://evil.example/logo.png"
    cases.append(x)
    x = copy.deepcopy(doc)
    x["extra"] = {"html": "<b>"}
    cases.append(x)
    for c in cases:
        assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=c)).status_code == 422
    # another tenant's uploaded asset path is refused too
    x = copy.deepcopy(doc)
    x["brand"]["logo_url"] = f"/media/t/{uuid.uuid4()}/brand/x.webp"
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=x)).status_code == 422


async def test_logo_upload_reencodes_and_rejects_bad_files(client, app, platform_headers, tmp_path):
    from app.core.storage import LocalStorage

    app.state.storage = LocalStorage(str(tmp_path))
    t = await _tenant(client, app, platform_headers)
    h = _h(t)
    r = await client.post(
        "/api/v1/admin/theme/images", headers=h, files={"file": ("logo.png", _png(), "image/png")}
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["url"].startswith(f"/media/t/{t['id']}/brand/") and out["url"].endswith(".webp")
    assert max(out["width"], out["height"]) <= 512
    saved = tmp_path / out["url"].removeprefix("/media/")
    assert saved.exists() and Image.open(saved).format == "WEBP"

    doc = preset_document("minimal").model_dump(mode="json", by_alias=True)
    doc["brand"]["logo_url"] = out["url"]
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)).status_code == 200

    fake = b"<svg onload=alert(1)>" + b"0" * 100
    assert (
        await client.post(
            "/api/v1/admin/theme/images", headers=h, files={"file": ("logo.png", fake, "image/png")}
        )
    ).status_code == 422
    polyglot = b"\x89PNG\r\n\x1a\n" + b"not really a png"
    assert (
        await client.post(
            "/api/v1/admin/theme/images",
            headers=h,
            files={"file": ("x.png", polyglot, "image/png")},
        )
    ).status_code == 422
    big = b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024 + 10)
    assert (
        await client.post(
            "/api/v1/admin/theme/images", headers=h, files={"file": ("x.jpg", big, "image/jpeg")}
        )
    ).status_code == 422


async def test_theme_permissions(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    owner = _h(t)
    for email, role in (("content@example.com", "content"), ("support@example.com", "support")):
        await client.post("/api/v1/admin/staff", headers=owner, json={"email": email, "role": role})
        await set_password(app, email, t["id"])
    content = {
        "authorization": f"Bearer {await login(client, t['primary_host'], 'content@example.com', 'staff')}",
        "host": t["primary_host"],
    }
    support = {
        "authorization": f"Bearer {await login(client, t['primary_host'], 'support@example.com', 'staff')}",
        "host": t["primary_host"],
    }
    assert (
        await client.post("/api/v1/admin/theme/publish", headers=content, json={})
    ).status_code == 200
    assert (await client.get("/api/v1/admin/theme", headers=support)).status_code == 403
    client.cookies.clear()


async def test_versions_are_immutable_and_isolated(client, app, platform_headers, settings):
    t1 = await _tenant(client, app, platform_headers)
    t2 = await _tenant(client, app, platform_headers)
    v = (await client.get("/api/v1/admin/theme/versions", headers=_h(t2))).json()[0]
    assert (
        await client.post(f"/api/v1/admin/theme/versions/{v['id']}/restore", headers=_h(t1))
    ).status_code == 404
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.tests.conftest import ADMIN_URL

    owner = create_async_engine(
        ADMIN_URL.rsplit("/", 1)[0] + "/" + settings.database_url.rsplit("/", 1)[1]
    )
    try:
        async with owner.connect() as conn:
            try:
                async with conn.begin():
                    await conn.execute(text("UPDATE theme_versions SET note='x'"))
                raise AssertionError("theme_versions mutable")
            except Exception as exc:  # noqa: BLE001
                assert "append-only" in str(exc)
    finally:
        await owner.dispose()


async def test_settings_and_onboarding(client, app, platform_headers):
    t = await _tenant(client, app, platform_headers)
    h = _h(t)
    ob = (await client.get("/api/v1/admin/onboarding", headers=h)).json()
    assert ob["ready_to_launch"] is False
    r = await client.patch(
        "/api/v1/admin/settings",
        headers=h,
        json={
            "name": "Nazia's Boutique",
            "support_phone": "01911223344",
            "support_email": "help@example.com",
            "default_locale": "en",
        },
    )
    assert r.status_code == 200 and r.json()["support_phone"] == "+8801911223344"
    assert (await client.get("/api/v1/store", headers={"host": t["primary_host"]})).json()[
        "name"
    ] == "Nazia's Boutique"
    items = {
        i["key"]: i["done"]
        for i in (await client.get("/api/v1/admin/onboarding", headers=h)).json()["items"]
    }
    assert items["store_contact"] is True and items["payment_methods"] is False
