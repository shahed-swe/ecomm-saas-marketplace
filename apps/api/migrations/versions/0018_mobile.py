"""mobile: per-tenant app config and releases, account deletion requests

Revision ID: 0018
Revises: 0017
White-label apps read their configuration from the API, so a tenant can rebrand without a release.
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_isolation ON {table} USING (tenant_id = app_current_tenant()) "
        f"WITH CHECK (tenant_id = app_current_tenant())"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app_releases (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          app text NOT NULL DEFAULT 'buyer' CHECK (app IN ('buyer','vendor')),
          platform text NOT NULL CHECK (platform IN ('ios','android')),
          latest_version text NOT NULL,
          min_supported_version text NOT NULL,
          store_url text,
          force_message text,
          released_at timestamptz NOT NULL DEFAULT now(),
          updated_by text NOT NULL,
          UNIQUE (tenant_id, app, platform)
        )"""
    )
    op.execute(
        """
        CREATE TABLE app_configs (
          tenant_id uuid NOT NULL,
          app text NOT NULL DEFAULT 'buyer' CHECK (app IN ('buyer','vendor')),
          bundle_id text,
          app_name text,
          icon_url text,
          splash_url text,
          crash_dsn text,
          maintenance boolean NOT NULL DEFAULT false,
          maintenance_message text,
          features jsonb NOT NULL DEFAULT '{}',
          updated_by text NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, app)
        )"""
    )
    # Play and the App Store both require an in-app route to delete an account.
    op.execute(
        """
        CREATE TABLE account_deletion_requests (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          user_id uuid NOT NULL,
          status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','cancelled','completed','refused')),
          reason text,
          scheduled_for timestamptz NOT NULL,
          completed_at timestamptz,
          refusal_reason text,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_deletion_pending ON account_deletion_requests (tenant_id, user_id) "
        "WHERE status = 'pending'"
    )
    for table in ("app_releases", "app_configs", "account_deletion_requests"):
        _rls(table)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON app_releases, app_configs, account_deletion_requests "
        "TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    for t in ("account_deletion_requests", "app_configs", "app_releases"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
