"""white-label app builds: per-tenant build manifests, build runs, store credentials references

Revision ID: 0019
Revises: 0018
Signing keys never live in the database: only the reference CI resolves from its own secret store.
"""

from alembic import op

revision = "0019"
down_revision = "0018"
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
        CREATE TABLE app_store_profiles (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          app text NOT NULL DEFAULT 'buyer' CHECK (app IN ('buyer','vendor')),
          android_package text,
          ios_bundle_id text,
          play_track text NOT NULL DEFAULT 'internal'
            CHECK (play_track IN ('internal','alpha','beta','production')),
          asc_app_id text,
          -- names of secrets in CI, never the secrets themselves
          android_signing_ref text,
          ios_signing_ref text,
          firebase_android_ref text,
          firebase_ios_ref text,
          store_listing jsonb NOT NULL DEFAULT '{}',
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','ready','live','paused')),
          updated_by text NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, app)
        )"""
    )
    op.execute(
        """
        CREATE TABLE app_builds (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          app text NOT NULL DEFAULT 'buyer' CHECK (app IN ('buyer','vendor')),
          platform text NOT NULL CHECK (platform IN ('ios','android')),
          version text NOT NULL,
          build_number int NOT NULL CHECK (build_number > 0),
          status text NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','building','succeeded','failed','uploaded','rejected')),
          ci_ref text,
          artifact_url text,
          store_status text,
          error text,
          requested_by text NOT NULL,
          started_at timestamptz,
          finished_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, app, platform, version, build_number)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_app_builds_queue ON app_builds (status, created_at) WHERE status = 'queued'"
    )
    for table in ("app_store_profiles", "app_builds"):
        _rls(table)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON app_store_profiles, app_builds TO app, platform; "
        "END IF; END $$;"
    )


def downgrade() -> None:
    for t in ("app_builds", "app_store_profiles"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
