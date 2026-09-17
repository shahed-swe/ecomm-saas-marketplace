"""theme engine: tenant_themes (draft + published pointer), immutable theme_versions

Revision ID: 0004
Revises: 0003
ADR 0012.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE theme_versions (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          number int NOT NULL,
          document jsonb NOT NULL,
          note text,
          created_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, number),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE TRIGGER theme_versions_append_only BEFORE UPDATE OR DELETE ON theme_versions "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )
    op.execute(
        """
        CREATE TABLE tenant_themes (
          tenant_id uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
          draft_document jsonb NOT NULL,
          draft_updated_at timestamptz NOT NULL DEFAULT now(),
          draft_updated_by text,
          published_version_id uuid,
          FOREIGN KEY (tenant_id, published_version_id) REFERENCES theme_versions (tenant_id, id)
        )"""
    )
    for t in ("theme_versions", "tenant_themes"):
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {t}_isolation ON {t} USING (tenant_id = app_current_tenant()) "
            "WITH CHECK (tenant_id = app_current_tenant())"
        )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT ON theme_versions TO app, platform; "
        "GRANT SELECT, INSERT, UPDATE ON tenant_themes TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_themes CASCADE")
    op.execute("DROP TABLE IF EXISTS theme_versions CASCADE")
