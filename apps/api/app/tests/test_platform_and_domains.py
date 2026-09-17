import uuid

from app.core.cache_keys import object_key, tkey
from app.core.security import Principal, create_access_token
from app.modules.domains.service import DnsRecords


async def test_platform_surface_hidden_from_tenant_tokens(client, world):
    r = await client.get("/platform/v1/tenants", headers=world["A"].staff.headers())
    assert r.status_code == 404


async def test_reserved_and_duplicate_slugs(client, platform_headers, world):
    r = await client.post(
        "/platform/v1/tenants", headers=platform_headers, json={"slug": "admin", "name": "Nope"}
    )
    assert r.status_code == 409
    slug = f"dup{uuid.uuid4().hex[:6]}"
    assert (
        await client.post(
            "/platform/v1/tenants", headers=platform_headers, json={"slug": slug, "name": "One"}
        )
    ).status_code == 201
    r = await client.post(
        "/platform/v1/tenants", headers=platform_headers, json={"slug": slug, "name": "Two"}
    )
    assert r.status_code == 409


async def test_unknown_host_is_404(client):
    r = await client.get("/api/v1/store", headers={"host": "nobody.test.local"})
    assert r.status_code == 404


async def test_store_info_resolves_by_host(client, world):
    r = await client.get("/api/v1/store", headers={"host": world["B"].host})
    assert r.status_code == 200
    assert r.json()["name"] == "Store B"


async def test_single_mode_tenant_has_house_vendor_and_cannot_add_vendors(
    client, platform_headers, settings
):
    r = await client.post(
        "/platform/v1/tenants",
        headers=platform_headers,
        json={"slug": f"solo{uuid.uuid4().hex[:6]}", "name": "Solo shop"},
    )
    body = r.json()
    assert body["store_mode"] == "single" and body["house_vendor_id"]
    staff = create_access_token(
        settings, Principal(sub="s", kind="tenant_staff", tid=body["id"], roles=("owner",))
    )
    h = {"authorization": f"Bearer {staff}", "host": body["primary_host"]}
    vendors = (await client.get("/api/v1/admin/vendors", headers=h)).json()
    assert [v["is_house"] for v in vendors] == [True]
    r = await client.post(
        "/api/v1/admin/vendors", headers=h, json={"slug": "second", "display_name": "Second"}
    )
    assert r.status_code == 409


async def test_switch_to_single_blocked_while_vendors_exist(client, platform_headers, world):
    r = await client.patch(
        f"/platform/v1/tenants/{world['A'].id}",
        headers=platform_headers,
        json={"store_mode": "single"},
    )
    assert r.status_code == 409


async def test_custom_domain_verification_and_tls_gate(client, app, platform_headers, settings):
    r = await client.post(
        "/platform/v1/tenants",
        headers=platform_headers,
        json={"slug": f"dom{uuid.uuid4().hex[:6]}", "name": "Domain shop"},
    )
    t = r.json()
    staff = create_access_token(
        settings, Principal(sub="s", kind="tenant_staff", tid=t["id"], roles=("owner",))
    )
    h = {"authorization": f"Bearer {staff}", "host": t["primary_host"]}
    host = f"www.shop{uuid.uuid4().hex[:6]}.com.bd"
    r = await client.post("/api/v1/admin/domains", headers=h, json={"host": host.upper()})
    assert r.status_code == 201
    dom = r.json()
    token = dom["dns"]["records"][0]["value"]

    # platform subdomains cannot be claimed as "custom"
    r2 = await client.post(
        "/api/v1/admin/domains", headers=h, json={"host": f"x.{settings.platform_root_domain}"}
    )
    assert r2.status_code == 409

    assert (await client.get("/internal/tls/ask", params={"domain": host})).status_code == 404
    assert (await client.get("/api/v1/store", headers={"host": host})).status_code == 404

    async def wrong(_h):
        return DnsRecords(txt=["other"], cname=[f"stores.{settings.platform_root_domain}"], a=[])

    app.state.dns_lookup = wrong
    r = await client.post(f"/api/v1/admin/domains/{dom['id']}/verify", headers=h)
    assert r.json()["status"] == "pending"

    async def right(_h):
        return DnsRecords(txt=[token], cname=[f"stores.{settings.platform_root_domain}"], a=[])

    app.state.dns_lookup = right
    r = await client.post(f"/api/v1/admin/domains/{dom['id']}/verify", headers=h)
    assert r.json()["status"] == "active"
    assert (await client.get("/internal/tls/ask", params={"domain": host})).status_code == 200
    assert (await client.get("/api/v1/store", headers={"host": host})).status_code == 200

    r = await client.post(f"/api/v1/admin/domains/{dom['id']}/primary", headers=h)
    assert r.status_code == 200 and r.json()["is_primary"] is True
    assert (await client.get("/api/v1/store", headers={"host": host})).json()[
        "primary_host"
    ] == host


async def test_domain_already_claimed_elsewhere_is_generic_conflict(client, world):
    A, B = world["A"], world["B"]
    host = f"claimed{uuid.uuid4().hex[:6]}.com"
    assert (
        await client.post("/api/v1/admin/domains", headers=A.staff.headers(), json={"host": host})
    ).status_code == 201
    r = await client.post("/api/v1/admin/domains", headers=B.staff.headers(), json={"host": host})
    assert r.status_code == 409
    assert "Store A" not in r.text and A.id not in r.text


async def test_cancelled_tenant_disappears_immediately(client, platform_headers, settings):
    r = await client.post(
        "/platform/v1/tenants",
        headers=platform_headers,
        json={"slug": f"bye{uuid.uuid4().hex[:6]}", "name": "Bye"},
    )
    t = r.json()
    assert (
        await client.get("/api/v1/store", headers={"host": t["primary_host"]})
    ).status_code == 200
    await client.patch(
        f"/platform/v1/tenants/{t['id']}", headers=platform_headers, json={"status": "cancelled"}
    )
    assert (
        await client.get("/api/v1/store", headers={"host": t["primary_host"]})
    ).status_code == 404


def test_cache_and_object_keys_require_tenant():
    import pytest

    assert tkey("t1", "theme", "published") == "t:t1:theme:published"
    assert object_key("t1", "media", "a.avif") == "t/t1/media/a.avif"
    with pytest.raises(ValueError):
        tkey("", "x")
    with pytest.raises(ValueError):
        tkey("t1", "bad key with spaces")


async def test_recheck_deactivates_domain_whose_dns_moved(client, app, platform_headers, settings):
    from app.modules.domains.service import recheck_active_domains

    r = await client.post(
        "/platform/v1/tenants",
        headers=platform_headers,
        json={"slug": f"mv{uuid.uuid4().hex[:6]}", "name": "Mover"},
    )
    t = r.json()
    staff = create_access_token(
        settings, Principal(sub="s", kind="tenant_staff", tid=t["id"], roles=("owner",))
    )
    h = {"authorization": f"Bearer {staff}", "host": t["primary_host"]}
    host = f"moved{uuid.uuid4().hex[:6]}.com"
    dom = (await client.post("/api/v1/admin/domains", headers=h, json={"host": host})).json()
    token = dom["dns"]["records"][0]["value"]

    async def good(_):
        return DnsRecords(txt=[token], cname=[], a=["203.0.113.10"])

    app.state.dns_lookup = good
    assert (await client.post(f"/api/v1/admin/domains/{dom['id']}/verify", headers=h)).json()[
        "status"
    ] == "active"
    assert (await client.get("/internal/tls/ask", params={"domain": host})).status_code == 200

    async def moved(h_):
        return DnsRecords(txt=[token] if h_ != host else [], cname=[], a=["198.51.100.1"])

    async with app.state.platform_db.sessionmaker() as s, s.begin():
        gone = await recheck_active_domains(
            s,
            moved,
            root_domain=settings.platform_root_domain,
            edge_ips=settings.edge_ips,
            redis=app.state.redis,
        )
    assert host in gone
    assert (await client.get("/internal/tls/ask", params={"domain": host})).status_code == 404
    assert (await client.get("/api/v1/store", headers={"host": host})).status_code == 404
