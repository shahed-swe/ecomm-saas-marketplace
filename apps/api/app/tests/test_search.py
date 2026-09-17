import asyncio
import statistics
import time
import uuid

import pytest
from sqlalchemy import text

from app.modules.catalog.search import build_tsquery, tokens
from app.tests.conftest import make_tenant


def test_tsquery_is_built_from_clean_tokens():
    assert tokens("Red SAREE!!  & | ' :*") == ["red", "saree"]
    q = build_tsquery(tokens("red saree"), {})
    assert q == "('red':*) & ('saree':* | 'sari':* | 'shari':* | 'শাড়ি':*)"
    assert (
        build_tsquery(tokens("'); drop table products; --"), {})
        == "('drop':*) & ('table':*) & ('products':*)"
    )


async def test_search_facets_synonyms_and_isolation(client, world):
    A, B = world["A"], world["B"]
    host = {"host": A.host}
    r = (
        await client.get("/api/v1/catalog/search", headers=host, params={"q": "cotton kurti"})
    ).json()
    assert {i["slug"] for i in r["items"]} == {"kurti-a1", "kurti-a2"} and r["total"] == 2
    assert {f["value"] for f in r["facets"]["stores"]} == {"v-a1", "v-a2"}
    assert r["facets"]["brands"] == [{"value": "aarong", "label": "Aarong", "count": 2}]
    assert r["attributes"] == {}  # material is not filterable
    # tenant synonym: kameez -> kurti
    assert (
        await client.get("/api/v1/catalog/search", headers=host, params={"q": "kameez"})
    ).json()["total"] == 2
    # built-in Bangla expansion
    assert (await client.get("/api/v1/catalog/search", headers=host, params={"q": "কুর্তি"})).json()[
        "total"
    ] == 2
    # typo corrected against the tenant's own words
    typo = (await client.get("/api/v1/catalog/search", headers=host, params={"q": "kurtti"})).json()
    assert typo["total"] == 2 and typo["did_you_mean"] == "kurti"
    # a store filter naming another tenant's store yields nothing
    assert (
        await client.get("/api/v1/catalog/search", headers=host, params={"store": "v-b1"})
    ).json()["total"] == 0
    # filters
    one = (
        await client.get(
            "/api/v1/catalog/search", headers=host, params={"q": "kurti", "store": "v-a2"}
        )
    ).json()
    assert [i["slug"] for i in one["items"]] == ["kurti-a2"]
    # tenant B's catalogue never appears in A's results or facets
    b = (
        await client.get("/api/v1/catalog/search", headers={"host": B.host}, params={"q": "kurti"})
    ).json()
    assert {i["slug"] for i in b["items"]} == {"kurti-b1", "kurti-b2"}
    assert all(f["value"].startswith("v-b") for f in b["facets"]["stores"])
    assert (
        await client.get("/api/v1/catalog/search", headers=host, params={"attr": "bad key:x"})
    ).status_code == 422


async def test_suggest(client, world):
    r = (
        await client.get(
            "/api/v1/catalog/suggest", headers={"host": world["A"].host}, params={"q": "kur"}
        )
    ).json()
    assert {p["slug"] for p in r["products"]} == {"kurti-a1", "kurti-a2"}


async def test_wishlist_and_recently_viewed(client, world):
    A = world["A"]
    reg = await client.post(
        "/api/v1/auth/register",
        headers={"host": A.host},
        json={"email": f"w{uuid.uuid4().hex[:6]}@example.com", "password": "correct-horse-battery"},
    )
    h = {"authorization": f"Bearer {reg.json()['access_token']}", "host": A.host}
    client.cookies.clear()
    p1, p2 = A.catalog["product"]["A1"], A.catalog["product"]["A2"]
    assert (
        await client.post("/api/v1/me/wishlist", headers=h, json={"product_id": p1})
    ).status_code == 204
    assert (
        await client.post("/api/v1/me/wishlist", headers=h, json={"product_id": p1})
    ).status_code == 204  # idempotent
    assert (
        await client.post(
            "/api/v1/me/wishlist",
            headers=h,
            json={"product_id": world["B"].catalog["product"]["B1"]},
        )
    ).status_code == 404
    assert [w["slug"] for w in (await client.get("/api/v1/me/wishlist", headers=h)).json()] == [
        "kurti-a1"
    ]
    for pid in (p1, p2, p1):
        await client.post("/api/v1/me/recently-viewed", headers=h, json={"product_id": pid})
    assert [
        x["slug"] for x in (await client.get("/api/v1/me/recently-viewed", headers=h)).json()
    ] == ["kurti-a1", "kurti-a2"]
    assert (
        await client.get("/api/v1/me/wishlist", headers=A.vendor_actors["A1"].headers())
    ).status_code == 403


@pytest.mark.perf
async def test_search_p95_under_200ms_at_20k_products_100_vendors(client, app, platform_headers):
    """Architecture §14 budget. Seeds 20k visible products across 100 vendors, plus a noisy second tenant."""
    t = await make_tenant(
        client,
        app,
        platform_headers,
        slug=f"big{uuid.uuid4().hex[:6]}",
        name="Big market",
        store_mode="multi",
    )
    words = [
        "cotton",
        "silk",
        "saree",
        "kurti",
        "panjabi",
        "shoe",
        "watch",
        "bag",
        "tea",
        "rice",
        "red",
        "blue",
        "jamdani",
        "tant",
        "premium",
        "classic",
        "summer",
        "eid",
        "wedding",
        "casual",
    ]
    async with app.state.platform_db.engine.begin() as conn:
        cat = (
            await conn.execute(
                text(
                    "INSERT INTO categories (tenant_id, slug, name_en) VALUES (:t, 'all', 'All') RETURNING id"
                ),
                {"t": t["id"]},
            )
        ).scalar()
        await conn.execute(
            text("""INSERT INTO brands (tenant_id, slug, name)
                                   SELECT :t, 'brand-' || g, 'Brand ' || g FROM generate_series(1, 40) g"""),
            {"t": t["id"]},
        )
        await conn.execute(
            text("""INSERT INTO vendors (tenant_id, slug, display_name, status)
                                   SELECT :t, 'shop-' || g, 'Shop ' || g, 'approved' FROM generate_series(1, 100) g"""),
            {"t": t["id"]},
        )
        await conn.execute(
            text(
                """WITH v AS (SELECT id, row_number() OVER () rn FROM vendors WHERE tenant_id = :t AND NOT is_house),
                    b AS (SELECT id, row_number() OVER () rn FROM brands WHERE tenant_id = :t)
               INSERT INTO products (tenant_id, vendor_id, category_id, brand_id, slug, title_en, description, status,
                                     moderation_status, min_price, max_price, in_stock, attributes)
               SELECT :t, (SELECT id FROM v WHERE rn = 1 + g % 100), :c, (SELECT id FROM b WHERE rn = 1 + g % 40),
                      'p-' || g,
                      (CAST(:w AS text[]))[1 + g % 20] || ' ' || (CAST(:w AS text[]))[1 + (g / 7) % 20] || ' item ' || g,
                      'Handmade ' || (CAST(:w AS text[]))[1 + (g / 3) % 20], 'active', 'approved',
                      100 + g % 5000, 150 + g % 5000, g % 9 <> 0, jsonb_build_object('colour', (ARRAY['red','blue','green'])[1 + g % 3])
               FROM generate_series(1, 20000) g"""
            ),
            {"t": t["id"], "c": cat, "w": words},
        )
        await conn.execute(
            text(
                """UPDATE products p SET search_vector = setweight(to_tsvector('simple', p.title_en), 'A')
                                   || setweight(to_tsvector('simple', b.name), 'B')
                                   || setweight(to_tsvector('simple', p.description), 'C')
               FROM brands b WHERE b.id = p.brand_id AND p.tenant_id = :t"""
            ),
            {"t": t["id"]},
        )
        tid = t["id"]
        await conn.execute(
            text(
                "INSERT INTO search_terms (tenant_id, word) SELECT CAST(:t AS uuid), word FROM ts_stat("
                + "'SELECT to_tsvector(''simple'', title_en) FROM products WHERE tenant_id = ''"
                + tid
                + "''') WHERE length(word) BETWEEN 2 AND 40"
            ),
            {"t": tid},
        )
        await conn.execute(text("ANALYZE products"))
    host = {"host": t["primary_host"]}
    queries = [
        {"q": "saree"},
        {"q": "red silk"},
        {"q": "kurti", "sort": "price_asc"},
        {"q": "premium watch", "in_stock": "true"},
        {"q": "jamdni"},
        {"q": "", "store": "shop-7"},
        {"q": "tea", "brand": "brand-3"},
        {"q": "শাড়ি"},
        {"q": "casual", "page": "5"},
        {"q": "eid wedding"},
    ]
    await client.get("/api/v1/catalog/search", headers=host, params=queries[0])  # warm caches
    timings = []
    for _ in range(4):
        for params in queries:
            start = time.perf_counter()
            r = await client.get("/api/v1/catalog/search", headers=host, params=params)
            timings.append((time.perf_counter() - start) * 1000)
            assert r.status_code == 200
    p95 = statistics.quantiles(timings, n=20)[18]
    print(
        f"search p50={statistics.median(timings):.1f}ms p95={p95:.1f}ms over {len(timings)} requests"
    )
    assert (await client.get("/api/v1/catalog/search", headers=host, params={"q": "saree"})).json()[
        "total"
    ] > 1000
    assert p95 < 200, timings
    await asyncio.sleep(0)


async def test_sitemap_lists_only_visible_tenant_urls(client, world):
    r = (await client.get("/api/v1/seo/sitemap", headers={"host": world["A"].host})).json()
    paths = {e["path"] for e in r["entries"]}
    assert {"/", "/p/kurti-a1", "/p/kurti-a2", "/c/fashion", "/store/v-a1"} <= paths
    assert not any("b1" in p or "b2" in p for p in paths)
    assert r["primary_host"] == world["A"].host
