import copy
import uuid

import pytest
from sqlalchemy import event

from app.modules.theme.css import CssRejected, sanitize_css
from app.tests.conftest import make_tenant


def _h(t):
    return {"authorization": f"Bearer {t['staff_token']}", "host": t["primary_host"]}


async def _draft(client, h):
    return (await client.get("/api/v1/admin/theme", headers=h)).json()["draft"]


def _sid():
    return uuid.uuid4().hex[:12]


async def test_starter_home_resolves_products(client, world):
    A = world["A"]
    r = await client.get("/api/v1/storefront/page", headers={"host": A.host})
    assert r.status_code == 200
    grid = next(s for s in r.json()["sections"] if s["type"] == "product_grid")
    slugs = {p["slug"] for p in grid["data"]["products"]}
    assert {"kurti-a1", "kurti-a2"} <= slugs
    assert all(not p["slug"].endswith(("b1", "b2")) for p in grid["data"]["products"])


async def test_home_query_budget_with_many_sections(client, app, world):
    A = world["A"]
    h = A.staff.headers()
    doc = await _draft(client, h)
    doc["templates"]["home"] = [
        {"id": _sid(), "type": "category_grid", "settings": {"category_slugs": ["fashion"]}},
        {"id": _sid(), "type": "brand_strip", "settings": {"brand_slugs": ["aarong"]}},
        {"id": _sid(), "type": "top_vendors", "settings": {"limit": 4}},
        {"id": _sid(), "type": "featured_vendor", "settings": {"store_slug": "v-a1"}},
        {
            "id": _sid(),
            "type": "product_carousel",
            "settings": {"query": {"source": "category", "category_slug": "fashion"}},
        },
        {
            "id": _sid(),
            "type": "product_carousel",
            "settings": {"query": {"source": "brand", "brand_slug": "aarong"}},
        },
        {
            "id": _sid(),
            "type": "product_carousel",
            "settings": {"query": {"source": "store", "store_slug": "v-a2"}},
        },
        {
            "id": _sid(),
            "type": "product_grid",
            "settings": {
                "query": {
                    "source": "manual",
                    "product_ids": [A.catalog["product"]["A2"], A.catalog["product"]["A1"]],
                }
            },
        },
        {"id": _sid(), "type": "rich_text", "settings": {"body": {"en": "Hello"}}},
    ]
    assert (
        await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)
    ).status_code == 200, "draft"
    tok = (await client.post("/api/v1/admin/theme/preview-token", headers=h)).json()["token"]
    count = {"n": 0}

    def before(conn, cursor, statement, *args):
        if "set_config" not in statement and "resolve_tenant_by_host" not in statement:
            count["n"] += 1

    engine = app.state.db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        r = await client.get(
            "/api/v1/storefront/page", headers={"host": A.host}, params={"preview": tok}
        )
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert r.status_code == 200, r.text
    secs = r.json()["sections"]
    manual = secs[7]["data"]["products"]
    assert [p["id"] for p in manual] == [
        A.catalog["product"]["A2"],
        A.catalog["product"]["A1"],
    ]  # order kept
    assert secs[3]["data"]["vendor"]["slug"] == "v-a1"
    assert [p["vendor_slug"] for p in secs[6]["data"]["products"]] == ["v-a2"]
    # 1 theme draft read + 4 data queries (products, categories, brands, vendors) ≤ 6
    assert count["n"] <= 6, count


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "marquee", "settings": {}},
        {
            "type": "rich_text",
            "settings": {"body": {"en": "x"}, "heading": {"en": "<b>"}},
            "extra": 1,
        },
        {"type": "image_banner", "settings": {"image_url": "https://evil.example/a.jpg"}},
        {
            "type": "campaign_strip",
            "settings": {"heading": {"en": "Eid"}, "link": {"href": "javascript:alert(1)"}},
        },
        {"type": "video_embed", "settings": {"url": "https://evil.example/video"}},
        {"type": "product_carousel", "settings": {"query": {"source": "category"}}},
    ],
)
async def test_invalid_sections_rejected(client, world, bad):
    h = world["A"].staff.headers()
    doc = await _draft(client, h)
    doc["templates"]["home"] = [{"id": _sid(), **bad}]
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)).status_code == 422


async def test_placement_limits_and_pages(client, world):
    h = world["A"].staff.headers()
    base = await _draft(client, h)
    doc = copy.deepcopy(base)
    doc["templates"]["product_bottom"] = [
        {
            "id": _sid(),
            "type": "hero_slider",
            "settings": {"slides": [{"image_url": f"/media/t/{world['A'].id}/x/a.webp"}]},
        }
    ]
    assert (
        await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)
    ).status_code == 422  # not on PDP
    doc = copy.deepcopy(base)
    doc["templates"]["home"] = [{"id": "sameid001", "type": "spacer", "settings": {}}] * 2
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)).status_code == 422
    doc = copy.deepcopy(base)
    doc["templates"]["home"] = [
        {"id": _sid(), "type": "announcement_bar", "settings": {"message": {"en": "a"}}}
        for _ in range(2)
    ]
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)).status_code == 422
    doc = copy.deepcopy(base)
    doc["pages"] = [{"slug": "checkout", "title": {"en": "x"}}]
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)).status_code == 422
    doc["pages"] = [
        {
            "slug": "about",
            "title": {"en": "About us"},
            "sections": [
                {
                    "id": _sid(),
                    "type": "faq",
                    "settings": {"items": [{"question": {"en": "COD?"}, "answer": {"en": "Yes"}}]},
                }
            ],
        },
        {"slug": "draft-page", "title": {"en": "Soon"}, "published": False},
    ]
    doc["layouts"]["header"]["menu"] = [
        {"label": {"en": "About", "bn": "আমাদের কথা"}, "href": "/pages/about"}
    ]
    doc["layouts"]["footer"]["social"] = {"facebook": "https://www.facebook.com/nazia"}
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)).status_code == 200
    bad_social = copy.deepcopy(doc)
    bad_social["layouts"]["footer"]["social"] = {"facebook": "https://evil.example/fb"}
    assert (
        await client.put("/api/v1/admin/theme/draft", headers=h, json=bad_social)
    ).status_code == 422
    host = {"host": world["A"].host}
    assert (
        await client.get(
            "/api/v1/storefront/page", headers=host, params={"template": "custom", "slug": "about"}
        )
    ).status_code == 404
    await client.post("/api/v1/admin/theme/publish", headers=h, json={})
    about = await client.get(
        "/api/v1/storefront/page", headers=host, params={"template": "custom", "slug": "about"}
    )
    assert about.status_code == 200 and about.json()["title"]["en"] == "About us"
    assert about.json()["layouts"]["header"]["menu"][0]["label"]["bn"] == "আমাদের কথা"
    assert (
        await client.get(
            "/api/v1/storefront/page",
            headers=host,
            params={"template": "custom", "slug": "draft-page"},
        )
    ).status_code == 404


async def test_preset_switch_keeps_content_and_rollback_restores_sections(
    client, app, platform_headers
):
    t = await make_tenant(
        client, app, platform_headers, slug=f"pb{uuid.uuid4().hex[:6]}", name="Builder"
    )
    h = _h(t)
    doc = await _draft(client, h)
    doc["templates"]["home"].append(
        {"id": "mysection01", "type": "spacer", "settings": {"size": "lg"}}
    )
    await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)
    v2 = (await client.post("/api/v1/admin/theme/publish", headers=h, json={})).json()
    switched = (
        await client.post("/api/v1/admin/theme/preset", headers=h, json={"preset": "fashion"})
    ).json()
    assert (
        switched["preset"] == "fashion" and switched["templates"]["home"][-1]["id"] == "mysection01"
    )
    doc = await _draft(client, h)
    doc["templates"]["home"] = []
    await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)
    await client.post("/api/v1/admin/theme/publish", headers=h, json={})
    assert (
        await client.get("/api/v1/storefront/page", headers={"host": t["primary_host"]})
    ).json()["sections"] == []
    await client.post(f"/api/v1/admin/theme/versions/{v2['id']}/restore", headers=h)
    ids = [
        s["id"]
        for s in (
            await client.get("/api/v1/storefront/page", headers={"host": t["primary_host"]})
        ).json()["sections"]
    ]
    assert "mysection01" in ids


def test_css_sanitizer():
    tid = "0190a0a0-0000-7000-8000-000000000001"
    out = sanitize_css(
        ":root{--x:1} body{margin:0} .hero h1, .card:hover{color:red;letter-spacing:.02em}"
        f"@media (max-width: 640px){{.hero{{background:url(/media/t/{tid}/x.webp)}}}}",
        tid,
    )
    assert "[data-tenant-css]{--x:1}" in out
    assert "[data-tenant-css] .hero h1,[data-tenant-css] .card:hover{" in out
    assert "@media (max-width: 640px){[data-tenant-css] .hero{" in out
    for bad in [
        "@import url(https://evil.example/x.css);",
        "@font-face{font-family:x;src:url(https://evil.example/f.woff)}",
        ".a{background:url(https://evil.example/track.gif)}",
        ".a{background:url(/media/t/0190a0a0-0000-7000-8000-000000000999/x.webp)}",
        ".a{width:expression(alert(1))}",
        ".overlay{position:fixed;inset:0}",
        ".a{z-index:99999}",
        "</style><script>alert(1)</script>",
        ".a{behavior:url(x.htc)}",
        "@keyframes spin{from{opacity:0}}",
        ".a{content:attr(data-token)}",
    ]:
        with pytest.raises(CssRejected):
            sanitize_css(bad, tid)


async def test_custom_css_is_plan_gated_and_served_scoped(client, app, platform_headers, world):
    starter = await make_tenant(
        client, app, platform_headers, slug=f"css{uuid.uuid4().hex[:6]}", name="Css shop"
    )
    doc = await _draft(client, _h(starter))
    doc["custom_css"] = ".hero{color:red}"
    assert (
        await client.put("/api/v1/admin/theme/draft", headers=_h(starter), json=doc)
    ).status_code == 402

    A = world["A"]  # growth plan
    h = A.staff.headers()
    doc = await _draft(client, h)
    doc["custom_css"] = ".product-card{border-radius:0}"
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)).status_code == 200
    doc["custom_css"] = ".x{position:fixed}"
    assert (await client.put("/api/v1/admin/theme/draft", headers=h, json=doc)).status_code == 422
    tok = (await client.post("/api/v1/admin/theme/preview-token", headers=h)).json()["token"]
    css = await client.get(
        "/api/v1/storefront/custom.css", headers={"host": A.host}, params={"preview": tok}
    )
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert css.text == "[data-tenant-css] .product-card{border-radius:0}"


async def test_vendor_store_builder(client, app, world):
    A = world["A"]
    vh = A.vendor_actors["A1"].headers()
    host = {"host": A.host}
    not_allowed = {"sections": [{"id": _sid(), "type": "top_vendors", "settings": {}}]}
    assert (
        await client.put("/api/v1/vendor/store-theme/draft", headers=vh, json=not_allowed)
    ).status_code == 422
    pale = {"accent": "250 250 250"}
    assert (await client.put("/api/v1/vendor/store-theme/draft", headers=vh, json=pale)).json()[
        "title"
    ] == "contrast_too_low"
    good = {
        "accent": "22 101 52",
        "sections": [
            {
                "id": _sid(),
                "type": "rich_text",
                "settings": {
                    "heading": {"en": "Handloom from Tangail"},
                    "body": {"en": "Since 1998"},
                },
            },
            {
                "id": _sid(),
                "type": "product_carousel",
                "settings": {"query": {"source": "store", "store_slug": "v-a1"}},
            },
        ],
    }
    assert (
        await client.put("/api/v1/vendor/store-theme/draft", headers=vh, json=good)
    ).status_code == 200
    before = (await client.get("/api/v1/storefront/stores/v-a1", headers=host)).json()
    assert before["sections"] == [] and before["accent"] is None
    await client.post("/api/v1/vendor/store-theme/publish", headers=vh)
    page = (await client.get("/api/v1/storefront/stores/v-a1", headers=host)).json()
    assert page["accent"] == "22 101 52" and page["sections"][0]["type"] == "rich_text"
    assert {p["slug"] for p in page["products"]} == {"kurti-a1"}
    assert (await client.get("/api/v1/storefront/stores/house", headers=host)).status_code == 404
    assert (
        await client.get("/api/v1/storefront/stores/v-a1", headers={"host": world["B"].host})
    ).status_code == 404
