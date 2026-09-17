"""fulfilment: courier accounts and rules, one shipment per sub-order, events, COD settlements

Revision ID: 0012
Revises: 0011
ADR 0007 (courier-only, normalised statuses, per-tenant credentials, settlement matching).
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

COURIERS = "('pathao','steadfast','redx')"
STATUSES = (
    "('booked','picked_up','in_transit','out_for_delivery','delivered','partial_delivered',"
    "'failed_attempt','returning','returned','cancelled')"
)


def _rls(table: str, vendor: bool) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cond = "tenant_id = app_current_tenant()"
    if vendor:
        cond += " AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor())"
    op.execute(f"CREATE POLICY {table}_isolation ON {table} USING ({cond}) WITH CHECK ({cond})")


def upgrade() -> None:
    # Courier area codes are reference data shared by every tenant (no RLS, read-only to the app).
    op.execute(
        """
        CREATE TABLE geo_courier_areas (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          courier text NOT NULL CHECK (courier IN """
        + COURIERS
        + """),
          district_code text NOT NULL REFERENCES geo_districts(code),
          area_name text NOT NULL,
          area_code text NOT NULL,
          zone_code text,
          UNIQUE (courier, district_code, area_name)
        )"""
    )
    op.execute(
        """
        CREATE TABLE courier_accounts (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          vendor_id uuid,
          courier text NOT NULL CHECK (courier IN """
        + COURIERS
        + """),
          mode text NOT NULL DEFAULT 'sandbox' CHECK (mode IN ('sandbox','live')),
          credentials_ciphertext text NOT NULL,
          key_version text NOT NULL DEFAULT 'v1',
          pickup_ref text,
          status text NOT NULL DEFAULT 'unverified'
            CHECK (status IN ('unverified','healthy','failing','disabled')),
          last_checked_at timestamptz,
          last_error text,
          updated_by text NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    # One tenant-level account per courier; a vendor may hold its own when the tenant allows it.
    op.execute(
        "CREATE UNIQUE INDEX uq_courier_account_tenant ON courier_accounts (tenant_id, courier) "
        "WHERE vendor_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_courier_account_vendor ON courier_accounts (tenant_id, vendor_id, courier) "
        "WHERE vendor_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE courier_rules (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          priority int NOT NULL DEFAULT 100,
          courier text NOT NULL CHECK (courier IN """
        + COURIERS
        + """),
          districts text[] NOT NULL DEFAULT '{}',
          zones text[] NOT NULL DEFAULT '{}',
          max_weight_grams int CHECK (max_weight_grams > 0),
          max_cod_amount numeric(12,2) CHECK (max_cod_amount >= 0),
          enabled boolean NOT NULL DEFAULT true,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute("CREATE INDEX ix_courier_rules_order ON courier_rules (tenant_id, priority)")
    op.execute(
        """
        CREATE TABLE shipments (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          order_id uuid NOT NULL,
          sub_order_id uuid NOT NULL,
          courier text NOT NULL CHECK (courier IN """
        + COURIERS
        + """),
          consignment_id text NOT NULL,
          tracking_code text,
          tracking_url text,
          label_key text,
          status text NOT NULL DEFAULT 'booked' CHECK (status IN """
        + STATUSES
        + """),
          status_detail text,
          cod_amount numeric(12,2) NOT NULL DEFAULT 0 CHECK (cod_amount >= 0),
          delivery_fee numeric(12,2),
          weight_grams int NOT NULL DEFAULT 0,
          failed_attempts int NOT NULL DEFAULT 0,
          needs_attention boolean NOT NULL DEFAULT false,
          attention_reason text,
          booked_at timestamptz NOT NULL DEFAULT now(),
          picked_up_at timestamptz,
          delivered_at timestamptz,
          returned_at timestamptz,
          last_event_at timestamptz,
          cancelled_at timestamptz,
          UNIQUE (tenant_id, courier, consignment_id),
          UNIQUE (tenant_id, id),
          FOREIGN KEY (tenant_id, sub_order_id) REFERENCES sub_orders (tenant_id, id),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_shipments_vendor ON shipments (tenant_id, vendor_id, booked_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_shipments_open ON shipments (last_event_at) "
        "WHERE status NOT IN ('delivered','returned','cancelled')"
    )
    op.execute(
        """
        CREATE TABLE shipment_events (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          shipment_id uuid,
          courier text NOT NULL,
          event_id text NOT NULL,
          raw_status text NOT NULL,
          status text CHECK (status IS NULL OR status IN """
        + STATUSES
        + """),
          payload jsonb NOT NULL,
          occurred_at timestamptz,
          received_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, courier, event_id)
        )"""
    )
    op.execute(
        "CREATE TRIGGER shipment_events_append_only BEFORE UPDATE OR DELETE ON shipment_events "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )
    op.execute(
        """
        CREATE TABLE courier_settlements (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          courier text NOT NULL CHECK (courier IN """
        + COURIERS
        + """),
          statement_ref text NOT NULL,
          period_start date,
          period_end date,
          total_amount numeric(12,2) NOT NULL DEFAULT 0,
          total_fee numeric(12,2) NOT NULL DEFAULT 0,
          matched_count int NOT NULL DEFAULT 0,
          unmatched_count int NOT NULL DEFAULT 0,
          mismatch_count int NOT NULL DEFAULT 0,
          imported_by text NOT NULL,
          imported_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, courier, statement_ref),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        """
        CREATE TABLE courier_settlement_lines (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          settlement_id uuid NOT NULL,
          consignment_id text NOT NULL,
          amount numeric(12,2) NOT NULL,
          fee numeric(12,2) NOT NULL DEFAULT 0,
          shipment_id uuid,
          status text NOT NULL CHECK (status IN ('matched','unmatched','mismatch','duplicate')),
          note text,
          UNIQUE (tenant_id, settlement_id, consignment_id),
          FOREIGN KEY (tenant_id, settlement_id) REFERENCES courier_settlements (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    for table in (
        "courier_accounts",
        "courier_rules",
        "shipment_events",
        "courier_settlements",
        "courier_settlement_lines",
    ):
        _rls(table, vendor=False)
    _rls("shipments", vendor=True)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON courier_accounts, courier_rules, shipments, "
        "courier_settlements, courier_settlement_lines TO app, platform; "
        "GRANT DELETE ON courier_rules, courier_accounts TO app, platform; "
        "GRANT SELECT, INSERT ON shipment_events TO app, platform; "
        "GRANT SELECT ON geo_courier_areas TO app, platform; END IF; END $$;"
    )
    # Delivery settles COD: the courier holds the cash from that moment (ADR 0003 timing).
    op.execute("ALTER TABLE cod_receivables ADD COLUMN IF NOT EXISTS shipment_id uuid")


def downgrade() -> None:
    op.execute("ALTER TABLE cod_receivables DROP COLUMN IF EXISTS shipment_id")
    for t in (
        "courier_settlement_lines",
        "courier_settlements",
        "shipment_events",
        "shipments",
        "courier_rules",
        "courier_accounts",
        "geo_courier_areas",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
