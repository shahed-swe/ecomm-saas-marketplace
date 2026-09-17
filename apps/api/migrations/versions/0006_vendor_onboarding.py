"""vendor onboarding & management: profile, KYC documents, state events, invites, commission rules,
payout methods (encrypted), payout holds, re-auth

Revision ID: 0006
Revises: 0005
ADR 0002, 0004, 0009, 0010.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TENANT = ["vendor_invite_codes", "commission_rules"]
VENDOR = ["vendor_documents", "vendor_events", "vendor_payout_methods", "payout_holds"]


def _rls(table: str, vendor: bool) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cond = "tenant_id = app_current_tenant()"
    if vendor:
        cond += " AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor())"
    op.execute(f"CREATE POLICY {table}_isolation ON {table} USING ({cond}) WITH CHECK ({cond})")


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vendors
          ADD COLUMN legal_name text,
          ADD COLUMN business_type text CHECK (business_type IN ('individual','proprietorship','partnership','company')),
          ADD COLUMN contact_email citext,
          ADD COLUMN contact_phone text CHECK (contact_phone ~ '^\\+8801[3-9][0-9]{8}$'),
          ADD COLUMN district text,
          ADD COLUMN tier text NOT NULL DEFAULT 'standard' CHECK (tier IN ('launch','standard','premium')),
          ADD COLUMN approved_at timestamptz,
          ADD COLUMN suspended_at timestamptz,
          ADD COLUMN status_reason text,
          ADD COLUMN agreement_accepted_at timestamptz
        """
    )
    op.execute(
        """
        ALTER TABLE vendor_storefronts
          ADD COLUMN logo_url text,
          ADD COLUMN banner_url text,
          ADD COLUMN shipping_policy text
        """
    )
    op.execute(
        """
        ALTER TABLE tenant_settings
          ADD COLUMN vendor_signup text NOT NULL DEFAULT 'invite_only'
            CHECK (vendor_signup IN ('closed','invite_only','open')),
          ADD COLUMN required_vendor_documents text[] NOT NULL
            DEFAULT ARRAY['trade_licence','nid','bank_proof']::text[],
          ADD COLUMN default_commission_rate numeric(6,4) NOT NULL DEFAULT 0.1000
            CHECK (default_commission_rate >= 0 AND default_commission_rate < 1)
        """
    )
    op.execute(
        """
        CREATE TABLE vendor_documents (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          doc_type text NOT NULL CHECK (doc_type IN ('trade_licence','nid','tin','bin','bank_proof','other')),
          storage_key text NOT NULL,
          content_type text NOT NULL,
          byte_size int NOT NULL CHECK (byte_size > 0 AND byte_size <= 10485760),
          number_hash text,
          number_last4 text,
          status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','expired')),
          rejection_reason text,
          reviewed_by text,
          reviewed_at timestamptz,
          expires_at date,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE,
          UNIQUE (tenant_id, storage_key)
        )"""
    )
    op.execute("CREATE INDEX ix_vendor_docs ON vendor_documents (tenant_id, vendor_id, doc_type)")
    op.execute(
        "CREATE INDEX ix_vendor_docs_hash ON vendor_documents (tenant_id, number_hash) WHERE number_hash IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE vendor_events (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          from_status text NOT NULL,
          to_status text NOT NULL,
          reason text,
          actor_kind text NOT NULL,
          actor_id text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        "CREATE TRIGGER vendor_events_append_only BEFORE UPDATE OR DELETE ON vendor_events "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )
    op.execute(
        """
        CREATE TABLE vendor_invite_codes (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          code text NOT NULL,
          tier text NOT NULL DEFAULT 'standard' CHECK (tier IN ('launch','standard','premium')),
          commission_rate numeric(6,4) CHECK (commission_rate >= 0 AND commission_rate < 1),
          commission_expires_at timestamptz,
          max_uses int NOT NULL DEFAULT 1 CHECK (max_uses > 0),
          used_count int NOT NULL DEFAULT 0 CHECK (used_count >= 0),
          expires_at timestamptz,
          created_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, code),
          CHECK (used_count <= max_uses)
        )"""
    )
    op.execute(
        """
        CREATE TABLE commission_rules (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          scope text NOT NULL CHECK (scope IN ('vendor','category')),
          scope_id uuid NOT NULL,
          rate numeric(6,4) NOT NULL CHECK (rate >= 0 AND rate < 1),
          starts_at timestamptz NOT NULL DEFAULT now(),
          expires_at timestamptz,
          created_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE INDEX ix_commission_scope ON commission_rules (tenant_id, scope, scope_id, starts_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE vendor_payout_methods (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          method text NOT NULL CHECK (method IN ('bank','bkash')),
          account_name text NOT NULL,
          details_ciphertext text NOT NULL,
          account_hash text NOT NULL,
          last4 text NOT NULL,
          bank_name text,
          branch_name text,
          routing_number text,
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','replaced')),
          created_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_payout_method_active ON vendor_payout_methods (tenant_id, vendor_id) "
        "WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX ix_payout_method_hash ON vendor_payout_methods (tenant_id, account_hash)"
    )
    op.execute(
        """
        CREATE TABLE payout_holds (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          reason text NOT NULL CHECK (reason IN ('destination_change','dispute','manual','kyc')),
          note text,
          hold_until timestamptz,
          released_at timestamptz,
          released_by text,
          created_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    for t in TENANT:
        _rls(t, vendor=False)
    for t in VENDOR:
        _rls(t, vendor=True)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON vendor_documents, vendor_invite_codes, commission_rules, "
        "vendor_payout_methods, payout_holds TO app, platform; "
        "GRANT SELECT, INSERT ON vendor_events TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    for t in [
        "payout_holds",
        "vendor_payout_methods",
        "commission_rules",
        "vendor_invite_codes",
        "vendor_events",
        "vendor_documents",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute(
        "ALTER TABLE tenant_settings DROP COLUMN vendor_signup, DROP COLUMN required_vendor_documents, "
        "DROP COLUMN default_commission_rate"
    )
    op.execute(
        "ALTER TABLE vendor_storefronts DROP COLUMN logo_url, DROP COLUMN banner_url, DROP COLUMN shipping_policy"
    )
    op.execute(
        "ALTER TABLE vendors DROP COLUMN legal_name, DROP COLUMN business_type, DROP COLUMN contact_email, "
        "DROP COLUMN contact_phone, DROP COLUMN district, DROP COLUMN tier, DROP COLUMN approved_at, "
        "DROP COLUMN suspended_at, DROP COLUMN status_reason, DROP COLUMN agreement_accepted_at"
    )
