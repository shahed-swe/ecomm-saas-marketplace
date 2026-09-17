"""identity: users per tenant, refresh families, OTP, staff roles/permissions, vendor users, audit log,
platform users

Revision ID: 0003
Revises: 0002
ADR 0014 (identity), architecture §13 (RBAC, audit).
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TENANT_TABLES = [
    "users",
    "refresh_tokens",
    "otp_challenges",
    "staff_roles",
    "staff_members",
    "audit_log",
]


def _grant(sql: str) -> None:
    op.execute(
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN {sql}; END IF; END $$;"
    )


def _rls(table: str, vendor: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cond = "tenant_id = app_current_tenant()"
    if vendor:
        cond += " AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor())"
    op.execute(f"CREATE POLICY {table}_isolation ON {table} USING ({cond}) WITH CHECK ({cond})")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          email citext,
          phone text CHECK (phone ~ '^\\+8801[3-9][0-9]{8}$'),
          password_hash text,
          full_name text,
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','deleted')),
          email_verified_at timestamptz,
          phone_verified_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          last_login_at timestamptz,
          CHECK (email IS NOT NULL OR phone IS NOT NULL),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_users_tenant_email ON users (tenant_id, email) WHERE email IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_users_tenant_phone ON users (tenant_id, phone) WHERE phone IS NOT NULL"
    )
    _rls("users")

    op.execute(
        """
        CREATE TABLE refresh_tokens (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          user_id uuid NOT NULL,
          family_id uuid NOT NULL,
          token_hash text NOT NULL UNIQUE,
          kind text NOT NULL CHECK (kind IN ('buyer','tenant_staff','vendor_staff')),
          vendor_id uuid,
          replaced_by uuid,
          revoked_at timestamptz,
          expires_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute("CREATE INDEX ix_refresh_family ON refresh_tokens (tenant_id, family_id)")
    _rls("refresh_tokens")

    op.execute(
        """
        CREATE TABLE otp_challenges (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          phone text NOT NULL,
          purpose text NOT NULL CHECK (purpose IN ('login','guest_checkout','reauth','verify_phone')),
          code_hash text NOT NULL,
          attempts int NOT NULL DEFAULT 0,
          consumed_at timestamptz,
          expires_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE INDEX ix_otp_lookup ON otp_challenges (tenant_id, phone, purpose, created_at DESC)"
    )
    _rls("otp_challenges")

    op.execute(
        """
        CREATE TABLE staff_roles (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          key text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{1,40}$'),
          name text NOT NULL,
          permissions text[] NOT NULL DEFAULT '{}',
          is_system boolean NOT NULL DEFAULT false,
          UNIQUE (tenant_id, key),
          UNIQUE (tenant_id, id)
        )"""
    )
    _rls("staff_roles")

    op.execute(
        """
        CREATE TABLE staff_members (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          user_id uuid NOT NULL,
          role_id uuid NOT NULL,
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('invited','active','disabled')),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, user_id),
          FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id, role_id) REFERENCES staff_roles (tenant_id, id)
        )"""
    )
    _rls("staff_members")

    op.execute(
        """
        CREATE TABLE vendor_users (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          user_id uuid NOT NULL,
          role text NOT NULL CHECK (role IN ('owner','manager','staff')),
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('invited','active','disabled')),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, vendor_id, user_id),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_vendor_one_owner ON vendor_users (tenant_id, vendor_id) WHERE role = 'owner'"
    )
    _rls("vendor_users", vendor=True)

    op.execute(
        """
        CREATE TABLE audit_log (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          actor_kind text NOT NULL,
          actor_id text NOT NULL,
          vendor_id uuid,
          action text NOT NULL,
          entity text NOT NULL,
          entity_id text,
          data jsonb NOT NULL DEFAULT '{}',
          ip inet,
          request_id text,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute("CREATE INDEX ix_audit_tenant_time ON audit_log (tenant_id, created_at DESC)")
    op.execute("CREATE INDEX ix_audit_entity ON audit_log (tenant_id, entity, entity_id)")
    _rls("audit_log")
    op.execute(
        "CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )

    op.execute(
        """
        CREATE TABLE platform_users (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          email citext NOT NULL UNIQUE,
          password_hash text NOT NULL,
          totp_secret text NOT NULL,
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    # No RLS policy and no app grant: only the platform role may touch platform_users.
    op.execute("ALTER TABLE platform_users ENABLE ROW LEVEL SECURITY")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='platform') THEN "
        "GRANT SELECT, INSERT, UPDATE ON platform_users TO platform; END IF; END $$;"
    )

    for t in TENANT_TABLES + ["vendor_users"]:
        if t == "audit_log":
            _grant("GRANT SELECT, INSERT ON audit_log TO app, platform")
        else:
            _grant(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO app, platform")


def downgrade() -> None:
    for t in [
        "platform_users",
        "audit_log",
        "vendor_users",
        "staff_members",
        "staff_roles",
        "otp_challenges",
        "refresh_tokens",
        "users",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
