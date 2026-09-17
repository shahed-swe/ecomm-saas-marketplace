"""vendor store theme (limited builder): accent, banner, allowed sections; draft + published

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE vendor_store_themes (
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          draft jsonb NOT NULL DEFAULT '{"accent": null, "banner_url": null, "sections": []}',
          published jsonb,
          published_at timestamptz,
          updated_at timestamptz NOT NULL DEFAULT now(),
          updated_by text,
          PRIMARY KEY (tenant_id, vendor_id),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute("ALTER TABLE vendor_store_themes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE vendor_store_themes FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY vendor_store_themes_isolation ON vendor_store_themes USING (tenant_id = app_current_tenant() "
        "AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor())) WITH CHECK (tenant_id = app_current_tenant() "
        "AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor()))"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON vendor_store_themes TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS vendor_store_themes")
