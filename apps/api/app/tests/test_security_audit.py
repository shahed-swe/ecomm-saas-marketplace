"""Phase 20: the audit that has to keep passing.

Everything here is a property of the *whole* system rather than of one feature, and each one is a
way a multi-tenant marketplace has actually leaked in the wild:

* a table shipped without RLS, or with RLS that its owner bypasses;
* an app database role that can see past its policies;
* a scoped endpoint that forgot its dependency and answers anyone;
* an append-only record that turned out to be editable;
* a signed URL cached by a proxy on the way out.
"""

import pytest
from sqlalchemy import text

# Tables that legitimately have no tenant_id: platform-wide reference data and billing.
PLATFORM_TABLES = {
    "alembic_version",
    "plans",
    "platform_admins",
    "platform_invoice_lines",
    "platform_invoices",
    "tenant_subscriptions",
    "usage_records",
    "geo_divisions",
    "geo_districts",
    "geo_courier_areas",
    "tenants",
    "domains",
}
# Records that must never be rewritten, and the column a rewrite would touch.
APPEND_ONLY = {
    "ledger_entries": "amount = amount + 1",
    "inventory_movements": "delta = delta + 1",
    "payment_events": "kind = 'tampered'",
    "shipment_events": "raw_status = 'tampered'",
    "messages": "body = 'tampered'",
    "store_credit_entries": "delta = delta + 1",
    "ticket_messages": "body = 'tampered'",
    "audit_log": "action = 'tampered'",
}


async def _tables(app) -> list[str]:
    async with app.state.platform_db.engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"""
                    )
                )
            )
            .scalars()
            .all()
        )
    return [str(r) for r in rows]


async def test_every_tenant_table_has_rls_enabled_and_forced(client, app, world):
    """A tenant table without FORCE is a table its owner reads straight through."""
    async with app.state.platform_db.engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                              (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies,
                              EXISTS (SELECT 1 FROM information_schema.columns col
                                      WHERE col.table_name = c.relname AND col.column_name = 'tenant_id')
                                AS has_tenant_id
                       FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"""
                    )
                )
            )
            .mappings()
            .all()
        )
    missing = [
        r["relname"]
        for r in rows
        if r["has_tenant_id"]
        and r["relname"] not in PLATFORM_TABLES
        and not (r["relrowsecurity"] and r["relforcerowsecurity"] and r["policies"])
    ]
    assert not missing, f"tenant tables without enforced RLS: {missing}"
    # and the two tables keyed by id rather than tenant_id still have their own policy
    by_id = {r["relname"]: r for r in rows if r["relname"] in ("tenants", "domains")}
    assert all(r["relrowsecurity"] and r["policies"] for r in by_id.values())


async def test_the_application_role_cannot_bypass_its_policies(app):
    async with app.state.platform_db.engine.begin() as conn:
        app_role = (
            (
                await conn.execute(
                    text("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'app'")
                )
            )
            .mappings()
            .one()
        )
    assert app_role["rolbypassrls"] is False and app_role["rolsuper"] is False


@pytest.mark.parametrize("table,mutation", sorted(APPEND_ONLY.items()))
async def test_append_only_records_refuse_to_be_rewritten(app, world, table, mutation):
    import sqlalchemy.exc

    async with app.state.platform_db.engine.begin() as conn:
        exists = await conn.scalar(
            text(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = :t"  # noqa: S608
            ),
            {"t": table},
        )
    assert exists, f"{table} is missing"
    with pytest.raises((sqlalchemy.exc.DatabaseError, sqlalchemy.exc.ProgrammingError)):
        async with app.state.platform_db.engine.begin() as conn:
            await conn.execute(text(f"UPDATE {table} SET {mutation}"))  # noqa: S608
    with pytest.raises((sqlalchemy.exc.DatabaseError, sqlalchemy.exc.ProgrammingError)):
        async with app.state.platform_db.engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608


async def test_no_scoped_route_answers_without_a_token(client, app, world):
    """Walks the live OpenAPI schema: every staff, vendor and platform route must refuse anonymity."""
    host = world["A"].host
    paths = app.openapi()["paths"]
    offenders = []
    for path, operations in paths.items():
        if not path.startswith(("/api/v1/admin", "/api/v1/vendor", "/platform/v1")):
            continue
        for method, spec in operations.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            url = path
            for parameter in spec.get("parameters", []):
                if parameter.get("in") == "path":
                    url = url.replace(
                        "{" + parameter["name"] + "}", "00000000-0000-7000-8000-000000000000"
                    )
            if "{" in url:
                url = url.split("{")[0].rstrip("/") + "/placeholder"
            response = await client.request(method.upper(), url, headers={"host": host}, json={})
            # 401 (no token) or 404 (the platform surface hiding itself) are both correct;
            # anything that smells like a result is not.
            if response.status_code not in (401, 403, 404, 405, 422):
                offenders.append(f"{method.upper()} {path} -> {response.status_code}")
    assert not offenders, f"routes reachable without authentication: {offenders}"


async def test_responses_carry_the_security_headers(client, world):
    response = await client.get("/api/v1/store", headers={"host": world["A"].host})
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    # signed URLs and personal data must not be cached by anything in the middle
    assert response.headers["cache-control"] == "no-store"


async def test_health_and_metrics_do_not_leak_tenant_data(client, world):
    health = await client.get("/healthz")
    assert health.status_code == 200
    body = health.text.lower()
    assert world["A"].id not in body and "postgres" not in body


async def test_an_unknown_host_is_simply_not_a_store(client):
    response = await client.get("/api/v1/store", headers={"host": "not-a-tenant.example.com"})
    assert response.status_code == 404
    assert "tenant" not in response.text.lower()


async def test_errors_never_carry_a_stack_trace(client, world):
    response = await client.get("/api/v1/me/orders/NOPE-404", headers={"host": world["A"].host})
    assert response.status_code in (401, 404)
    assert "Traceback" not in response.text and "sqlalchemy" not in response.text.lower()
