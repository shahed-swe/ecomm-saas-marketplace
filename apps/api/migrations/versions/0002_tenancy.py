"""tenancy: tenants, domains, vendors (house vendor), tenant settings, vendor storefronts, RLS

Revision ID: 0002
Revises: 0001
ADR 0001 (two-level tenancy), ADR 0002 (house vendor).
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _grant(sql: str) -> None:
    # Roles exist in every real environment (infra/postgres/init); guard for bare databases.
    op.execute(
        f"""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app') THEN {sql}; END IF;
        END $$;"""
    )


def _tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant())"""
    )


def _vendor_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""CREATE POLICY {table}_tenant_vendor_isolation ON {table}
            USING (tenant_id = app_current_tenant()
                   AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor()))
            WITH CHECK (tenant_id = app_current_tenant()
                   AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor()))"""
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenants (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          public_id text NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(9), 'hex'),
          slug citext NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$'),
          name text NOT NULL CHECK (length(name) BETWEEN 2 AND 120),
          status text NOT NULL DEFAULT 'trial'
            CHECK (status IN ('trial','active','past_due','suspended','cancelled','purged')),
          store_mode text NOT NULL DEFAULT 'single' CHECK (store_mode IN ('single','multi')),
          plan_code text,
          default_locale text NOT NULL DEFAULT 'bn' CHECK (default_locale IN ('bn','en')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    # tenants is keyed by id, not tenant_id: a tenant may read only its own row.
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenants_self ON tenants USING (id = app_current_tenant()) "
        "WITH CHECK (id = app_current_tenant())"
    )

    op.execute(
        """
        CREATE TABLE domains (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          host citext NOT NULL UNIQUE CHECK (host ~ '^[a-z0-9.-]{3,253}$'),
          kind text NOT NULL CHECK (kind IN ('subdomain','custom')),
          status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','verified','active','inactive')),
          is_primary boolean NOT NULL DEFAULT false,
          verification_token text NOT NULL DEFAULT encode(gen_random_bytes(16), 'hex'),
          verified_at timestamptz,
          last_checked_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute("CREATE INDEX ix_domains_tenant ON domains (tenant_id)")
    op.execute("CREATE UNIQUE INDEX uq_domains_one_primary ON domains (tenant_id) WHERE is_primary")
    _tenant_rls("domains")

    op.execute(
        """
        CREATE TABLE vendors (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          slug citext NOT NULL,
          display_name text NOT NULL,
          status text NOT NULL DEFAULT 'registered'
            CHECK (status IN ('registered','documents_submitted','under_review',
                              'changes_requested','approved','rejected','suspended','closed')),
          is_house boolean NOT NULL DEFAULT false,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, slug),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute("CREATE UNIQUE INDEX uq_vendors_one_house ON vendors (tenant_id) WHERE is_house")
    # vendors: tenant-scoped; in vendor context a vendor sees only its own row.
    op.execute("ALTER TABLE vendors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE vendors FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY vendors_isolation ON vendors
           USING (tenant_id = app_current_tenant()
                  AND (app_current_vendor() IS NULL OR id = app_current_vendor()))
           WITH CHECK (tenant_id = app_current_tenant()
                  AND (app_current_vendor() IS NULL OR id = app_current_vendor()))"""
    )

    op.execute(
        """
        CREATE TABLE tenant_settings (
          tenant_id uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
          support_email citext,
          support_phone text,
          timezone text NOT NULL DEFAULT 'Asia/Dhaka',
          currency char(3) NOT NULL DEFAULT 'BDT',
          updated_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    _tenant_rls("tenant_settings")

    op.execute(
        """
        CREATE TABLE vendor_storefronts (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          tagline text,
          bio text,
          return_policy text,
          holiday_mode boolean NOT NULL DEFAULT false,
          updated_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE,
          UNIQUE (tenant_id, vendor_id)
        )"""
    )
    _vendor_rls("vendor_storefronts")

    # Host -> tenant resolution happens BEFORE a tenant is known, so it cannot run under RLS.
    # A SECURITY DEFINER function exposes exactly one lookup and nothing else.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_tenant_by_host(p_host text)
        RETURNS TABLE (tenant_id uuid, tenant_status text, domain_status text, is_primary boolean,
                       primary_host citext)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
          SELECT t.id, t.status, d.status, d.is_primary,
                 (SELECT p.host FROM domains p WHERE p.tenant_id = t.id AND p.is_primary)
          FROM domains d JOIN tenants t ON t.id = d.tenant_id
          WHERE d.host = lower(p_host)
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION resolve_tenant_by_host(text) FROM PUBLIC")
    _grant("GRANT EXECUTE ON FUNCTION resolve_tenant_by_host(text) TO app, platform")
    # Caddy on-demand TLS 'ask': is this host an active domain? Nothing else is revealed.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tls_host_allowed(p_host text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
          SELECT EXISTS (
            SELECT 1 FROM domains d JOIN tenants t ON t.id = d.tenant_id
            WHERE d.host = lower(p_host) AND d.status = 'active'
              AND t.status NOT IN ('cancelled','purged'))
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION tls_host_allowed(text) FROM PUBLIC")
    _grant("GRANT EXECUTE ON FUNCTION tls_host_allowed(text) TO app, platform")

    for t in ("tenants", "domains", "vendors", "tenant_settings", "vendor_storefronts"):
        _grant(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO app, platform")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS tls_host_allowed(text)")
    op.execute("DROP FUNCTION IF EXISTS resolve_tenant_by_host(text)")
    for t in ("vendor_storefronts", "tenant_settings", "vendors", "domains", "tenants"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
