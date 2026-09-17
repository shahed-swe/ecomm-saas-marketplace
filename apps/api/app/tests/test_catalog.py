import asyncio
import io
import uuid

from PIL import Image
from sqlalchemy import event, text

from app.tests.conftest import jpeg_bytes, make_tenant


def _h(t):
    return {"authorization": f"Bearer {t['staff_token']}", "host": t["primary_host"]}


async def _shop(client, app, platform_headers, moderation="first_listing"):
    """Single-vendor store: the owner is also the house vendor owner."""
    from app.tests.conftest import login

    t = await make_tenant(
        client,
        app,
        platform_headers,
        slug=f"cat{uuid.uuid4().hex[:6]}",
        name="Catalog shop",
        store_mode="multi",
    )
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_settings SET moderation_mode = :m WHERE tenant_id = :t"),
            {"m": moderation, "t": t["id"]},
        )
    staff = _h(t)
    cat = (
        await client.post(
            "/api/v1/admin/catalog/categories",
            headers=staff,
            json={"slug": "women", "name_en": "Women"},
        )
    ).json()
    sub = (
        await client.post(
            "/api/v1/admin/catalog/categories",
            headers=staff,
            json={"slug": "sarees", "name_en": "Sarees", "name_bn": "শাড়ি", "parent_id": cat["id"]},
        )
    ).json()
    await client.post(
        f"/api/v1/admin/catalog/categories/{cat['id']}/attributes",
        headers=staff,
        json={
            "key": "fabric",
            "label_en": "Fabric",
            "type": "select",
            "options": ["cotton", "silk"],
            "required": True,
            "filterable": True,
        },
    )
    # a real vendor (non-house) owned by a separate user
    r = await client.post(
        "/api/v1/admin/vendors",
        headers=staff,
        json={"slug": "tant-ghor", "display_name": "Tant Ghor", "owner_email": "tant@example.com"},
    )
    vid = r.json()["id"]
    from app.tests.conftest import set_password

    await set_password(app, "tant@example.com", t["id"])
    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(text("UPDATE vendors SET status='approved' WHERE id=:v"), {"v": vid})
    vtok = await login(client, t["primary_host"], "tant@example.com", "vendor", vid)
    client.cookies.clear()
    return t, {"authorization": f"Bearer {vtok}", "host": t["primary_host"]}, cat, sub, vid


def _product(cat_id, slug, sku="SAREE-1", **kw):
    body = {
        "slug": slug,
        "title_en": "Jamdani Saree",
        "category_id": cat_id,
        "attributes": {"fabric": "cotton"},
        "variants": [{"sku": sku, "options": {"Colour": "Red"}, "price": "4500.00", "stock": 3}],
    }
    body.update(kw)
    return body


async def _publishable(client, vh, cat_id, slug, sku):
    r = await client.post("/api/v1/vendor/products", headers=vh, json=_product(cat_id, slug, sku))
    assert r.status_code == 201, r.text
    asset = (
        await client.post(
            "/api/v1/vendor/media",
            headers=vh,
            files={
                "file": ("x.jpg", jpeg_bytes(color=(len(slug) * 7 % 255, 10, 10)), "image/jpeg")
            },
        )
    ).json()
    await client.post(
        f"/api/v1/vendor/products/{r.json()['id']}/media",
        headers=vh,
        json={"asset_id": asset["id"]},
    )
    return r.json()["id"]


async def test_taxonomy_rules(client, app, platform_headers):
    t, _, cat, sub, _ = await _shop(client, app, platform_headers)
    staff = _h(t)
    assert (
        await client.post(
            "/api/v1/admin/catalog/categories",
            headers=staff,
            json={"slug": "women", "name_en": "Again"},
        )
    ).status_code == 409
    parent = sub["id"]
    for i in range(2):
        parent = (
            await client.post(
                "/api/v1/admin/catalog/categories",
                headers=staff,
                json={"slug": f"deep{i}", "name_en": f"Deep {i}", "parent_id": parent},
            )
        ).json()["id"]
    too_deep = await client.post(
        "/api/v1/admin/catalog/categories",
        headers=staff,
        json={"slug": "deep9", "name_en": "Too deep", "parent_id": parent},
    )
    assert too_deep.status_code == 422
    assert (
        await client.post(
            f"/api/v1/admin/catalog/categories/{cat['id']}/attributes",
            headers=staff,
            json={"key": "size", "label_en": "Size", "type": "select"},
        )
    ).status_code == 422
    tree = (
        await client.get("/api/v1/catalog/categories", headers={"host": t["primary_host"]})
    ).json()
    women = next(n for n in tree if n["slug"] == "women")
    assert women["children"][0]["slug"] == "sarees" and women["children"][0]["name_bn"] == "শাড়ি"
    inherited = (
        await client.get(
            "/api/v1/catalog/categories/sarees/attributes", headers={"host": t["primary_host"]}
        )
    ).json()
    assert [a["key"] for a in inherited] == ["fabric"]


async def test_product_validation(client, app, platform_headers, world):
    t, vh, cat, sub, _ = await _shop(client, app, platform_headers)
    bad_attr = _product(sub["id"], "x1", attributes={"fabric": "polyester"})
    assert (
        await client.post("/api/v1/vendor/products", headers=vh, json=bad_attr)
    ).status_code == 422
    unknown = _product(sub["id"], "x2", attributes={"colourway": "red"})
    assert (
        await client.post("/api/v1/vendor/products", headers=vh, json=unknown)
    ).status_code == 422
    price = _product(sub["id"], "x3")
    price["variants"][0]["compare_at_price"] = "4000.00"
    assert (await client.post("/api/v1/vendor/products", headers=vh, json=price)).status_code == 422
    dup = _product(sub["id"], "x4")
    dup["variants"].append({**dup["variants"][0], "sku": "OTHER"})
    assert (await client.post("/api/v1/vendor/products", headers=vh, json=dup)).status_code == 422
    html = _product(sub["id"], "x5", description="<script>alert(1)</script>Handwoven <b>cotton</b>")
    r = await client.post("/api/v1/vendor/products", headers=vh, json=html)
    assert r.status_code == 201 and "<" not in r.json()["description"]
    # slug taken by another vendor in the same tenant -> generic conflict
    staff_house = await client.post(
        "/api/v1/admin/vendors",
        headers=_h(t),
        json={"slug": "other-v", "display_name": "Other", "owner_email": "ov@example.com"},
    )
    assert staff_house.status_code == 201
    same = await client.post(
        "/api/v1/vendor/products", headers=vh, json=_product(sub["id"], "x5", sku="NEW-SKU")
    )
    assert same.status_code == 409 and "Tant" not in same.text
    # a vendor from another tenant cannot use this tenant's category
    other = world["A"].vendor_actors["A1"].headers()
    assert (
        await client.post("/api/v1/vendor/products", headers=other, json=_product(sub["id"], "x6"))
    ).status_code == 404


async def test_publish_moderation_and_visibility(client, app, platform_headers):
    t, vh, cat, sub, vid = await _shop(client, app, platform_headers)
    host = {"host": t["primary_host"]}
    bare = await client.post(
        "/api/v1/vendor/products", headers=vh, json=_product(sub["id"], "no-photo", "NP-1")
    )
    assert (
        await client.patch(
            f"/api/v1/vendor/products/{bare.json()['id']}", headers=vh, json={"status": "active"}
        )
    ).json()["title"] == "photo_required"

    pid = await _publishable(client, vh, sub["id"], "first-saree", "FS-1")
    r = await client.patch(f"/api/v1/vendor/products/{pid}", headers=vh, json={"status": "active"})
    assert r.json()["moderation_status"] == "pending"
    assert (
        await client.get("/api/v1/catalog/products/first-saree", headers=host)
    ).status_code == 404
    queue = (await client.get("/api/v1/admin/moderation/products", headers=_h(t))).json()
    assert [q["slug"] for q in queue] == ["first-saree"]
    assert (
        await client.post(
            f"/api/v1/admin/products/{pid}/moderate", headers=_h(t), json={"decision": "reject"}
        )
    ).status_code == 422
    await client.post(
        f"/api/v1/admin/products/{pid}/moderate", headers=_h(t), json={"decision": "approve"}
    )
    pdp = await client.get("/api/v1/catalog/products/first-saree", headers=host)
    assert pdp.status_code == 200
    body = pdp.json()
    assert body["vendor"]["display_name"] == "Tant Ghor" and body["category"]["slug"] == "sarees"
    assert body["media"][0]["renditions"]["avif"]

    second = await _publishable(client, vh, sub["id"], "second-saree", "SS-1")
    r = await client.patch(
        f"/api/v1/vendor/products/{second}", headers=vh, json={"status": "active"}
    )
    assert r.json()["moderation_status"] == "approved"  # first listing already approved

    # listing includes parent category filter and keyset pagination
    page = (
        await client.get(
            "/api/v1/catalog/products", headers=host, params={"category": "women", "limit": 1}
        )
    ).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    page2 = (
        await client.get(
            "/api/v1/catalog/products",
            headers=host,
            params={"category": "women", "limit": 1, "cursor": page["next_cursor"]},
        )
    ).json()
    assert {page["items"][0]["slug"], page2["items"][0]["slug"]} == {"first-saree", "second-saree"}

    # suspending the vendor removes products from the storefront
    await client.post(
        f"/api/v1/admin/vendors/{vid}/transition",
        headers=_h(t),
        json={"to": "suspended", "reason": "quality"},
    )
    assert (await client.get("/api/v1/catalog/products", headers=host)).json()["items"] == []
    assert (
        await client.get("/api/v1/catalog/products/first-saree", headers=host)
    ).status_code == 404


async def test_pdp_query_budget(client, app, platform_headers):
    t, vh, cat, sub, vid = await _shop(client, app, platform_headers, moderation="none")
    pid = await _publishable(client, vh, sub["id"], "budget-saree", "BS-1")
    await client.patch(f"/api/v1/vendor/products/{pid}", headers=vh, json={"status": "active"})
    count = {"n": 0}

    def before(conn, cursor, statement, *args):
        if "set_config" not in statement and "resolve_tenant_by_host" not in statement:
            count["n"] += 1

    engine = app.state.db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        r = await client.get(
            "/api/v1/catalog/products/budget-saree", headers={"host": t["primary_host"]}
        )
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert r.status_code == 200
    assert count["n"] <= 3, count


async def test_stock_is_correct_under_concurrency_and_ledgered(client, app, platform_headers):
    from httpx import ASGITransport, AsyncClient

    t, vh, cat, sub, vid = await _shop(client, app, platform_headers)
    r = await client.post(
        "/api/v1/vendor/products", headers=vh, json=_product(sub["id"], "stocky", "STK-1")
    )
    variant = r.json()["variants"][0]["id"]

    async def take():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return (
                await c.post(
                    f"/api/v1/vendor/variants/{variant}/stock", headers=vh, json={"delta": -2}
                )
            ).status_code

    results = await asyncio.gather(*(take() for _ in range(3)))
    assert sorted(results) == [200, 409, 409]  # stock 3: only one -2 fits
    async with app.state.platform_db.engine.connect() as conn:
        on_hand = (
            await conn.execute(
                text("SELECT stock_on_hand FROM product_variants WHERE id = :v"), {"v": variant}
            )
        ).scalar()
        ledger = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(delta),0) FROM inventory_movements WHERE variant_id = :v"
                ),
                {"v": variant},
            )
        ).scalar()
    assert on_hand == 1 and ledger == 1


async def test_price_change_revalidates_storefront(client, app, platform_headers):
    t, vh, cat, sub, vid = await _shop(client, app, platform_headers)
    r = await client.post(
        "/api/v1/vendor/products", headers=vh, json=_product(sub["id"], "priced", "PR-1")
    )
    variant = r.json()["variants"][0]["id"]
    app.state.revalidator.calls.clear()
    await client.patch(f"/api/v1/vendor/variants/{variant}", headers=vh, json={"price": "3999.00"})
    tags = app.state.revalidator.calls[-1]
    assert f"t:{t['id']}:product:priced" in tags and f"t:{t['id']}:products" in tags
    assert r.json()["min_price"] == "4500.00"
    again = (await client.get(f"/api/v1/vendor/products/{r.json()['id']}", headers=vh)).json()
    assert again["min_price"] == "3999.00"


async def test_media_pipeline(client, app, platform_headers, tmp_path):
    from app.core.storage import LocalStorage

    app.state.storage = LocalStorage(str(tmp_path))
    t, vh, cat, sub, vid = await _shop(client, app, platform_headers)
    img = Image.new("RGB", (2400, 1600), (200, 50, 50))
    exif = Image.Exif()
    exif[0x8825] = {2: (23.0, 45.0, 0.0)}  # GPSInfo
    exif[0x010F] = "SecretPhoneMaker"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    r = await client.post(
        "/api/v1/vendor/media",
        headers=vh,
        files={"file": ("big.jpg", buf.getvalue(), "image/jpeg")},
    )
    assert r.status_code == 202, r.text
    a = r.json()
    assert a["status"] == "ready" and a["width"] == 2400
    assert sorted(a["renditions"]["avif"], key=int) == ["160", "400", "800", "1600"]
    assert list(a["renditions"]["jpeg"]) == ["800"] and a["blur_data"].startswith(
        "data:image/webp;base64,"
    )
    card = tmp_path / a["renditions"]["avif"]["400"].removeprefix("/media/")
    out = Image.open(card)
    assert out.format == "AVIF" and out.width == 400 and not out.getexif()
    assert card.stat().st_size < 30_000
    assert (
        b"SecretPhoneMaker"
        not in (tmp_path / a["renditions"]["jpeg"]["800"].removeprefix("/media/")).read_bytes()
    )

    small = io.BytesIO()
    Image.new("RGBA", (300, 200), (0, 0, 0, 0)).save(small, "PNG")
    s = (
        await client.post(
            "/api/v1/vendor/media",
            headers=vh,
            files={"file": ("s.png", small.getvalue(), "image/png")},
        )
    ).json()
    assert list(s["renditions"]["webp"]) == ["160"]  # never upscaled

    dup = (
        await client.post(
            "/api/v1/vendor/media",
            headers=vh,
            files={"file": ("again.jpg", buf.getvalue(), "image/jpeg")},
        )
    ).json()
    assert (
        dup["status"] == "ready"
        and dup["renditions"] == a["renditions"]
        and "duplicate" in dup["error"]
    )

    assert (
        await client.post(
            "/api/v1/vendor/media",
            headers=vh,
            files={"file": ("x.jpg", b"GIF89a....", "image/jpeg")},
        )
    ).status_code == 422
    broken = await client.post(
        "/api/v1/vendor/media",
        headers=vh,
        files={"file": ("x.jpg", b"\xff\xd8\xff\xe0 truncated", "image/jpeg")},
    )
    assert broken.json()["status"] == "failed"


async def test_csv_import(client, app, platform_headers):
    t, vh, cat, sub, vid = await _shop(client, app, platform_headers)
    csv_body = (
        "handle,title_en,title_bn,category,sku,option1_name,option1_value,price,compare_at_price,stock\n"
        "silk-saree,Silk Saree,সিল্ক শাড়ি,sarees,SILK-R,Colour,Red,6500,,4\n"
        "silk-saree,Silk Saree,,sarees,SILK-B,Colour,Blue,6600,7000,2\n"
        "bad-row,Bad,,no-such-category,BAD-1,,,100,,1\n"
        "bad-price,Bad price,,sarees,BAD-2,,,abc,,1\n"
    ).encode()
    r = await client.post(
        "/api/v1/vendor/imports", headers=vh, files={"file": ("p.csv", csv_body, "text/csv")}
    )
    job = r.json()
    assert job["status"] == "done" and job["succeeded"] == 2 and job["failed"] == 2
    assert {e["row"] for e in job["errors"]} == {4, 5}
    products = (await client.get("/api/v1/vendor/products", headers=vh)).json()
    silk = next(p for p in products if p["slug"] == "silk-saree")
    assert len(silk["variants"]) == 2 and silk["title_bn"] == "সিল্ক শাড়ি"

    update = (
        b"handle,title_en,category,sku,price,stock\nsilk-saree,Silk Saree,sarees,SILK-R,6200,10\n"
    )
    job2 = (
        await client.post(
            "/api/v1/vendor/imports", headers=vh, files={"file": ("u.csv", update, "text/csv")}
        )
    ).json()
    assert job2["succeeded"] == 1
    silk = next(
        p
        for p in (await client.get("/api/v1/vendor/products", headers=vh)).json()
        if p["slug"] == "silk-saree"
    )
    red = next(v for v in silk["variants"] if v["sku"] == "SILK-R")
    assert red["price"] == "6200.00" and red["stock_on_hand"] == 10
    missing = (
        await client.post(
            "/api/v1/vendor/imports",
            headers=vh,
            files={"file": ("m.csv", b"title_en,price\nX,1\n", "text/csv")},
        )
    ).json()
    assert missing["status"] == "failed" and "Missing columns" in missing["errors"][0]["error"]


async def test_questions_and_answers(client, app, platform_headers, world):
    A = world["A"]
    host = {"host": A.host}
    assert (
        await client.get("/api/v1/catalog/products/kurti-a1/questions", headers=host)
    ).json() == []
    qid = A.catalog["question"]["A1"]
    # A2 cannot answer A1's question (vendor scoped)
    assert (
        await client.post(
            f"/api/v1/vendor/questions/{qid}/answer",
            headers=A.vendor_actors["A2"].headers(),
            json={"answer": "no"},
        )
    ).status_code == 404
    r = await client.post(
        f"/api/v1/vendor/questions/{qid}/answer",
        headers=A.vendor_actors["A1"].headers(),
        json={"answer": "Yes <i>fits</i> well"},
    )
    assert r.status_code == 200
    published = (
        await client.get("/api/v1/catalog/products/kurti-a1/questions", headers=host)
    ).json()
    assert published[0]["answer"] == "Yes fits well"
    # vendors cannot ask as buyers
    assert (
        await client.post(
            "/api/v1/catalog/products/kurti-a1/questions",
            headers=A.vendor_actors["A2"].headers(),
            json={"question": "Can I ask?"},
        )
    ).status_code == 403
