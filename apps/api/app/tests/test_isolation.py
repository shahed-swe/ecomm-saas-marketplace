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
    ("GET", "/api/v1/admin/billing/invoices/{id}", STAFF, "invoice", None),
    ("GET", "/api/v1/admin/vendors/{id}/review", STAFF, "vendor", None),
    ("POST", "/api/v1/admin/vendors/{id}/transition", STAFF, "vendor", {"to": "closed"}),
    ("POST", "/api/v1/admin/vendors/{id}/commission", STAFF, "vendor", {"rate": "0.05"}),
    ("PATCH", "/api/v1/admin/vendor-documents/{id}", STAFF, "document", {"status": "approved"}),
    ("POST", "/api/v1/admin/payout-holds/{id}/release", STAFF, "payout_hold", None),
    # catalog (Phase 7)
    ("GET", "/api/v1/vendor/products/{id}", VENDOR, "product", None),
    (
        "PATCH",
        "/api/v1/vendor/products/{id}",
        VENDOR,
        "product",
        {"title_en": "Cotton Kurti A1 renamed"},
    ),
    (
        "POST",
        "/api/v1/vendor/products/{id}/variants",
        VENDOR,
        "product",
        {"sku": "ISO-OWN-L", "options": {"Size": "L"}, "price": "1300.00"},
    ),
    (
        "POST",
        "/api/v1/vendor/products/{id}/media",
        VENDOR,
        "product",
        {"asset_id": "00000000-0000-7000-8000-000000000000"},
    ),
    ("PATCH", "/api/v1/vendor/variants/{id}", VENDOR, "variant", {"price": "1200.00"}),
    ("POST", "/api/v1/vendor/variants/{id}/stock", VENDOR, "variant", {"delta": 1}),
    ("DELETE", "/api/v1/vendor/product-media/{id}", VENDOR, "product_media", None),
    ("GET", "/api/v1/vendor/media/{id}", VENDOR, "asset", None),
    ("GET", "/api/v1/vendor/imports/{id}", VENDOR, "import_job", None),
    (
        "POST",
        "/api/v1/vendor/questions/{id}/answer",
        VENDOR,
        "question",
        {"answer": "Yes, true to size."},
    ),
    ("PATCH", "/api/v1/admin/catalog/categories/{id}", STAFF, "category", {"position": 1}),
    (
        "POST",
        "/api/v1/admin/catalog/categories/{id}/attributes",
        STAFF,
        "category",
        {"key": "iso_attr", "label_en": "Iso", "type": "text"},
    ),
    ("DELETE", "/api/v1/admin/catalog/category-attributes/{id}", STAFF, "attribute", None),
    (
        "PATCH",
        "/api/v1/admin/catalog/brands/{id}",
        STAFF,
        "brand",
        {"slug": "aarong", "name": "Aarong"},
    ),
    ("POST", "/api/v1/admin/products/{id}/moderate", STAFF, "product", {"decision": "approve"}),
    ("POST", "/api/v1/admin/questions/{id}/hide", STAFF, "question", None),
    ("DELETE", "/api/v1/admin/catalog/synonyms/{id}", STAFF, "synonym", None),
    # checkout (Phase 10)
    ("GET", "/api/v1/vendor/orders/{id}", VENDOR, "sub_order", None),
    ("POST", "/api/v1/vendor/coupons/{id}/deactivate", VENDOR, "coupon", None),
    (
        "POST",
        "/api/v1/vendor/campaigns/{id}/products",
        VENDOR,
        "campaign",
        {
            "variant_id": "00000000-0000-7000-8000-000000000000",
            "campaign_price": "1",
            "stock_cap": 1,
        },
    ),
    ("POST", "/api/v1/admin/campaigns/{id}/cancel", STAFF, "campaign", None),
    # payments + fulfilment (Phases 11-12)
    ("POST", "/api/v1/vendor/orders/{id}/ready", VENDOR, "sub_order", None),
    ("POST", "/api/v1/vendor/orders/{id}/ship", VENDOR, "sub_order", {}),
    ("GET", "/api/v1/vendor/shipments/{id}", VENDOR, "shipment", None),
    ("POST", "/api/v1/vendor/shipments/{id}/cancel", VENDOR, "shipment", None),
    ("GET", "/api/v1/admin/shipments/{id}", STAFF, "shipment", None),
    ("POST", "/api/v1/admin/shipments/{id}/resolve", STAFF, "shipment", {"note": "checked"}),
    # returns and refunds (Phase 13)
    ("GET", "/api/v1/vendor/returns/{id}", VENDOR, "return", None),
    ("POST", "/api/v1/vendor/returns/{id}/decision", VENDOR, "return", {"approve": True}),
    ("POST", "/api/v1/vendor/returns/{id}/pickup", VENDOR, "return", {}),
    ("POST", "/api/v1/vendor/returns/{id}/mark", VENDOR, "return", {"status": "received"}),
    ("POST", "/api/v1/vendor/returns/{id}/qc", VENDOR, "return", {"passed": True}),
    ("GET", "/api/v1/admin/returns/{id}", STAFF, "return", None),
    ("POST", "/api/v1/admin/returns/{id}/decision", STAFF, "return", {"approve": True}),
    ("POST", "/api/v1/admin/returns/{id}/refund", STAFF, "return", None),
    ("POST", "/api/v1/admin/returns/{id}/cancel", STAFF, "return", None),
    ("POST", "/api/v1/admin/refunds/{id}/complete", STAFF, "refund", {"reference": "TRX-1"}),
    # finance (Phase 14)
    ("GET", "/api/v1/admin/payout-batches/{id}", STAFF, "payout_batch", None),
    ("POST", "/api/v1/admin/payout-batches/{id}/approve", STAFF, "payout_batch", None),
    ("GET", "/api/v1/admin/payout-batches/{id}/export", STAFF, "payout_batch", None),
    ("POST", "/api/v1/admin/payout-lines/{id}/paid", STAFF, "payout_line", {"reference": "TRX-9"}),
    ("POST", "/api/v1/admin/payout-lines/{id}/failed", STAFF, "payout_line", {"reason": "bounced"}),
]

# Own-resource expectation where 200 is not the right answer (e.g. owners cannot edit themselves).
OWN_STATUS = {
    ("PATCH", "/api/v1/vendor/staff/{id}"): 409,
    ("POST", "/api/v1/vendor/orders/{id}/ready"): 409,  # the world order is still unpaid
    ("POST", "/api/v1/vendor/orders/{id}/ship"): 409,
    ("POST", "/api/v1/vendor/shipments/{id}/cancel"): 409,  # no courier account configured
    # the world's return is already refunded: every transition out of it is a conflict
    ("POST", "/api/v1/vendor/returns/{id}/decision"): 409,
    ("POST", "/api/v1/vendor/returns/{id}/pickup"): 409,
    ("POST", "/api/v1/vendor/returns/{id}/mark"): 409,
    ("POST", "/api/v1/vendor/returns/{id}/qc"): 409,
    ("POST", "/api/v1/vendor/products/{id}/variants"): 201,
    ("POST", "/api/v1/vendor/products/{id}/media"): 404,  # placeholder asset id does not exist
    (
        "POST",
        "/api/v1/vendor/campaigns/{id}/products",
    ): 404,  # placeholder variant id does not exist
    ("POST", "/api/v1/vendor/coupons/{id}/deactivate"): 204,
    ("DELETE", "/api/v1/vendor/product-media/{id}"): 204,
}


def _resource(world_t, kind, name):
    if kind == "domain":
        return world_t.domain_id
    if kind == "staff_member":
        return world_t.owner_staff_member_id
    if kind == "theme_version":
        return world_t.theme_version_id
    if kind == "invoice":
        return world_t.invoice_id
    if kind == "document":
        return world_t.document_id
    if kind == "payout_hold":
        return world_t.payout_hold_id
    if kind in world_t.catalog:
        ids = world_t.catalog[kind]
        return ids.get(name) or ids["tenant"]
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
