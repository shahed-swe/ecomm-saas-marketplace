"""Mandatory 2x2 isolation harness (ADR 0001, architecture §3.2).

ISO-T: tenant A cannot reach tenant B.   ISO-V: vendor A1 cannot reach vendor A2.
Every scoped route with a path id MUST be registered in SCOPED_ROUTES — test_every_scoped_route_is_covered
fails the build otherwise, so a new endpoint cannot ship without its isolation test.
"""

import pytest

VENDOR = "vendor"
STAFF = "staff"

# (method, path template, actor kind, resource kind, json body)
SCOPED_ROUTES = [
    ("GET", "/api/v1/vendor/storefronts/{id}", VENDOR, "storefront", None),
    ("PATCH", "/api/v1/vendor/storefronts/{id}", VENDOR, "storefront", {"tagline": "hi"}),
    ("GET", "/api/v1/admin/vendors/{id}", STAFF, "vendor", None),
    ("GET", "/api/v1/admin/storefronts/{id}", STAFF, "storefront", None),
    ("POST", "/api/v1/admin/domains/{id}/verify", STAFF, "domain", None),
    ("POST", "/api/v1/admin/domains/{id}/primary", STAFF, "domain", None),
    ("PATCH", "/api/v1/admin/staff/{id}", STAFF, "staff_member", {"status": "active"}),
    ("PATCH", "/api/v1/vendor/staff/{id}", VENDOR, "vendor_user", {"status": "active"}),
    ("POST", "/api/v1/admin/theme/versions/{id}/restore", STAFF, "theme_version", None),
]

# Own-resource expectation where 200 is not the right answer (e.g. owners cannot edit themselves).
OWN_STATUS = {("PATCH", "/api/v1/vendor/staff/{id}"): 409}


def _resource(world_t, kind, name):
    if kind == "domain":
        return world_t.domain_id
    if kind == "staff_member":
        return world_t.owner_staff_member_id
    if kind == "theme_version":
        return world_t.theme_version_id
    table = {
        "storefront": world_t.storefronts,
        "vendor": world_t.vendors,
        "vendor_user": world_t.vendor_owner_memberships,
    }[kind]
    return table[name]


@pytest.mark.parametrize("method,path,actor,kind,body", SCOPED_ROUTES)
async def test_iso_t_cross_tenant_is_404(client, world, method, path, actor, kind, body):
    A, B = world["A"], world["B"]
    who = A.vendor_actors["A1"] if actor == VENDOR else A.staff
    target = _resource(B, kind, "B1")
    r = await client.request(method, path.format(id=target), headers=who.headers(), json=body)
    assert r.status_code == 404, r.text


@pytest.mark.parametrize(
    "method,path,actor,kind,body", [x for x in SCOPED_ROUTES if x[2] == VENDOR]
)
async def test_iso_v_cross_vendor_same_tenant_is_404(
    client, world, method, path, actor, kind, body
):
    A = world["A"]
    target = _resource(A, kind, "A2")
    r = await client.request(
        method, path.format(id=target), headers=A.vendor_actors["A1"].headers(), json=body
    )
    assert r.status_code == 404, r.text


@pytest.mark.parametrize(
    "method,path,actor,kind,body", [x for x in SCOPED_ROUTES if x[2] == VENDOR]
)
async def test_own_resource_is_reachable(client, world, method, path, actor, kind, body):
    A = world["A"]
    target = _resource(A, kind, "A1")
    r = await client.request(
        method, path.format(id=target), headers=A.vendor_actors["A1"].headers(), json=body
    )
    assert r.status_code == OWN_STATUS.get((method, path), 200), r.text


@pytest.mark.parametrize("method,path,actor,kind,body", SCOPED_ROUTES)
async def test_token_replayed_on_other_tenant_host_is_401(
    client, world, method, path, actor, kind, body
):
    A, B = world["A"], world["B"]
    who = A.vendor_actors["A1"] if actor == VENDOR else A.staff
    target = _resource(B, kind, "B1")
    r = await client.request(
        method, path.format(id=target), headers=who.headers(host=B.host), json=body
    )
    assert r.status_code == 401, r.text


async def test_vendor_token_cannot_use_staff_routes(client, world):
    A = world["A"]
    r = await client.get(
        f"/api/v1/admin/vendors/{A.vendors['A1']}", headers=A.vendor_actors["A1"].headers()
    )
    assert r.status_code == 403


async def test_every_scoped_route_is_covered(app):
    """Walks the OpenAPI schema (FastAPI wraps included routers, so app.routes is not flat)."""
    import re

    paths = app.openapi()["paths"]
    assert len(paths) > 10  # guard against silently iterating nothing
    covered = {(m, p) for m, p, *_ in SCOPED_ROUTES}
    missing = []
    for path, ops in paths.items():
        if not path.startswith(("/api/v1/vendor", "/api/v1/admin")) or "{" not in path:
            continue
        template = re.sub(r"\{[^}]+\}", "{id}", path)
        for method in ops:
            if (method.upper(), template) not in covered:
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"scoped routes without isolation tests: {missing}"
