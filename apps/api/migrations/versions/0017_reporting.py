"""reporting: daily rollups per tenant and vendor, export jobs

Revision ID: 0017
Revises: 0016
Dashboards read rollups, not the order tree: a tenant's reports must stay fast as it grows.
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _rls(table: str, vendor: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cond = "tenant_id = app_current_tenant()"
    if vendor:
        cond += " AND (app_current_vendor() IS NULL OR vendor_id IS NULL OR vendor_id = app_current_vendor())"
    op.execute(f"CREATE POLICY {table}_isolation ON {table} USING ({cond}) WITH CHECK ({cond})")


def upgrade() -> None:
    # vendor_id NULL = the tenant's own total for that day; a vendor row is that vendor's slice.
    op.execute(
        """
        CREATE TABLE daily_metrics (
          tenant_id uuid NOT NULL,
          day date NOT NULL,
          vendor_id uuid,
          orders int NOT NULL DEFAULT 0,
          shipments int NOT NULL DEFAULT 0,
          units int NOT NULL DEFAULT 0,
          gmv numeric(14,2) NOT NULL DEFAULT 0,
          discounts numeric(14,2) NOT NULL DEFAULT 0,
          shipping numeric(14,2) NOT NULL DEFAULT 0,
          vat numeric(14,2) NOT NULL DEFAULT 0,
          commission numeric(14,2) NOT NULL DEFAULT 0,
          refunds numeric(14,2) NOT NULL DEFAULT 0,
          cancelled int NOT NULL DEFAULT 0,
          returned int NOT NULL DEFAULT 0,
          delivered int NOT NULL DEFAULT 0,
          cod_orders int NOT NULL DEFAULT 0,
          new_buyers int NOT NULL DEFAULT 0,
          computed_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    # Exactly one tenant-total row per day, and one row per vendor per day. A NULL vendor_id
    # cannot live in a primary key, so the rule is two partial unique indexes instead.
    op.execute(
        "CREATE UNIQUE INDEX uq_daily_metrics_tenant_total ON daily_metrics (tenant_id, day) "
        "WHERE vendor_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_daily_metrics_vendor ON daily_metrics (tenant_id, day, vendor_id) "
        "WHERE vendor_id IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_daily_metrics_day ON daily_metrics (tenant_id, day DESC)")
    op.execute(
        """
        CREATE TABLE report_exports (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid,
          kind text NOT NULL CHECK (kind IN ('orders','payouts','products','ledger','vendors','customers')),
          params jsonb NOT NULL DEFAULT '{}',
          status text NOT NULL DEFAULT 'ready' CHECK (status IN ('queued','ready','failed')),
          rows int NOT NULL DEFAULT 0,
          object_key text,
          error text,
          requested_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    for table in ("daily_metrics", "report_exports"):
        _rls(table, vendor=True)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON daily_metrics, report_exports TO app, platform; "
        "GRANT DELETE ON daily_metrics TO app, platform; "
        "END IF; END $$;"
    )


def downgrade() -> None:
    for t in ("report_exports", "daily_metrics"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
