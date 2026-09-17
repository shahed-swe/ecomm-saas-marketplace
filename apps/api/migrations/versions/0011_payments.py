"""payments: tenant gateway accounts, payments, provider events, COD receivables

Revision ID: 0011
Revises: 0010
ADR 0006 (tenant-owned accounts, verify-first, idempotent callbacks).
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE payment_accounts (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          provider text NOT NULL CHECK (provider IN ('bkash','sslcommerz')),
          mode text NOT NULL DEFAULT 'sandbox' CHECK (mode IN ('sandbox','live')),
          credentials_ciphertext text NOT NULL,
          key_version text NOT NULL DEFAULT 'v1',
          status text NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified','healthy','failing','disabled')),
          last_checked_at timestamptz,
          last_error text,
          updated_by text NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, provider)
        )"""
    )
    op.execute(
        """
        CREATE TABLE payments (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          order_id uuid NOT NULL,
          provider text NOT NULL CHECK (provider IN ('bkash','sslcommerz','cod')),
          amount numeric(12,2) NOT NULL CHECK (amount > 0),
          currency char(3) NOT NULL DEFAULT 'BDT',
          status text NOT NULL DEFAULT 'initiated'
            CHECK (status IN ('initiated','pending','paid','failed','cancelled','refunded','partially_refunded')),
          provider_ref text,
          payer_ref text,
          fee numeric(12,2) NOT NULL DEFAULT 0 CHECK (fee >= 0),
          verified_at timestamptz,
          failure_reason text,
          attempt int NOT NULL DEFAULT 1 CHECK (attempt > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, provider, provider_ref),
          UNIQUE (tenant_id, order_id, attempt),
          FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id)
        )"""
    )
    op.execute("CREATE INDEX ix_payments_order ON payments (tenant_id, order_id, created_at DESC)")
    op.execute(
        "CREATE UNIQUE INDEX uq_payment_paid_per_order ON payments (tenant_id, order_id) "
        "WHERE status IN ('paid','refunded','partially_refunded')"
    )
    op.execute(
        """
        CREATE TABLE payment_events (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          provider text NOT NULL,
          event_id text NOT NULL,
          payment_id uuid,
          kind text NOT NULL,
          payload jsonb NOT NULL,
          received_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, provider, event_id)
        )"""
    )
    op.execute(
        "CREATE TRIGGER payment_events_append_only BEFORE UPDATE OR DELETE ON payment_events "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )
    op.execute(
        """
        CREATE TABLE cod_receivables (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          order_id uuid NOT NULL,
          sub_order_id uuid NOT NULL UNIQUE,
          amount numeric(12,2) NOT NULL CHECK (amount >= 0),
          status text NOT NULL DEFAULT 'due' CHECK (status IN ('due','collected','settled','cancelled','written_off')),
          courier text,
          collected_at timestamptz,
          settled_at timestamptz,
          settlement_ref text,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, sub_order_id) REFERENCES sub_orders (tenant_id, id)
        )"""
    )
    for t, vendor in (("payments", False), ("payment_events", False), ("cod_receivables", True)):
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        cond = "tenant_id = app_current_tenant()"
        if vendor:
            cond += " AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor())"
        op.execute(f"CREATE POLICY {t}_isolation ON {t} USING ({cond}) WITH CHECK ({cond})")
    # Credentials are stored as ciphertext: a SELECT leaks nothing without the data key (Vault in production).
    op.execute("ALTER TABLE payment_accounts ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY payment_accounts_isolation ON payment_accounts USING (tenant_id = app_current_tenant()) "
        "WITH CHECK (tenant_id = app_current_tenant())"
    )
    # A provider webhook arrives on the platform host with no tenant Host header: the tenant's
    # public id in the path is resolved by one SECURITY DEFINER lookup that reveals nothing else.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_tenant_by_public_id(p_public_id text)
        RETURNS TABLE (tenant_id uuid, tenant_status text)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
          SELECT t.id, t.status FROM tenants t
          WHERE t.public_id = p_public_id AND t.status NOT IN ('cancelled','purged')
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION resolve_tenant_by_public_id(text) FROM PUBLIC")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON payments, payment_accounts, cod_receivables TO app, platform; "
        "GRANT SELECT, INSERT ON payment_events TO app, platform; "
        "GRANT EXECUTE ON FUNCTION resolve_tenant_by_public_id(text) TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_tenant_by_public_id(text)")
    for t in ("cod_receivables", "payment_events", "payments", "payment_accounts"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
