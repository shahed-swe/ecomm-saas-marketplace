"""catalog: tenant taxonomy (categories, attributes), brands, products, variants, inventory ledger,
media assets + pipeline state, product media, Q&A, CSV import jobs

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TENANT_TABLES = ["categories", "category_attributes", "brands"]
VENDOR_TABLES = [
    "products",
    "product_variants",
    "inventory_movements",
    "product_media",
    "import_jobs",
    "product_questions",
]


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
        ALTER TABLE tenant_settings
          ADD COLUMN moderation_mode text NOT NULL DEFAULT 'first_listing'
            CHECK (moderation_mode IN ('none','first_listing','all'))
        """
    )
    op.execute(
        """
        CREATE TABLE categories (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          parent_id uuid,
          slug citext NOT NULL CHECK (slug ~ '^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$'),
          name_en text NOT NULL,
          name_bn text,
          depth int NOT NULL DEFAULT 0 CHECK (depth BETWEEN 0 AND 3),
          path uuid[] NOT NULL DEFAULT '{}',
          position int NOT NULL DEFAULT 0,
          is_active boolean NOT NULL DEFAULT true,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, slug),
          UNIQUE (tenant_id, id),
          FOREIGN KEY (tenant_id, parent_id) REFERENCES categories (tenant_id, id)
        )"""
    )
    op.execute("CREATE INDEX ix_categories_path ON categories USING gin (path)")
    op.execute(
        """
        CREATE TABLE category_attributes (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          category_id uuid NOT NULL,
          key text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{0,40}$'),
          label_en text NOT NULL,
          label_bn text,
          type text NOT NULL CHECK (type IN ('text','number','select','multiselect','boolean')),
          options text[] NOT NULL DEFAULT '{}',
          unit text,
          required boolean NOT NULL DEFAULT false,
          filterable boolean NOT NULL DEFAULT false,
          position int NOT NULL DEFAULT 0,
          UNIQUE (tenant_id, category_id, key),
          FOREIGN KEY (tenant_id, category_id) REFERENCES categories (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        """
        CREATE TABLE brands (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          slug citext NOT NULL CHECK (slug ~ '^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$'),
          name text NOT NULL,
          logo_url text,
          is_active boolean NOT NULL DEFAULT true,
          UNIQUE (tenant_id, slug),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        """
        CREATE TABLE products (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          category_id uuid NOT NULL,
          brand_id uuid,
          slug citext NOT NULL CHECK (slug ~ '^[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?$'),
          title_en text NOT NULL CHECK (length(title_en) BETWEEN 3 AND 200),
          title_bn text CHECK (title_bn IS NULL OR length(title_bn) <= 200),
          description text NOT NULL DEFAULT '' CHECK (length(description) <= 20000),
          attributes jsonb NOT NULL DEFAULT '{}',
          status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','archived')),
          moderation_status text NOT NULL DEFAULT 'pending'
            CHECK (moderation_status IN ('pending','approved','rejected')),
          moderation_reason text,
          min_price numeric(12,2),
          max_price numeric(12,2),
          in_stock boolean NOT NULL DEFAULT false,
          weight_grams int CHECK (weight_grams IS NULL OR weight_grams > 0),
          quality_score real NOT NULL DEFAULT 0,
          search_vector tsvector,
          published_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, slug),
          UNIQUE (tenant_id, id),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id),
          FOREIGN KEY (tenant_id, category_id) REFERENCES categories (tenant_id, id),
          FOREIGN KEY (tenant_id, brand_id) REFERENCES brands (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_products_vendor ON products (tenant_id, vendor_id, updated_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_products_listing ON products (tenant_id, category_id, id DESC) "
        "WHERE status = 'active' AND moderation_status = 'approved'"
    )
    op.execute(
        "CREATE INDEX ix_products_moderation ON products (tenant_id, moderation_status, updated_at)"
    )
    op.execute(
        """
        CREATE TABLE product_variants (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          product_id uuid NOT NULL,
          sku text NOT NULL CHECK (sku ~ '^[A-Za-z0-9._-]{1,64}$'),
          options jsonb NOT NULL DEFAULT '{}',
          option_signature text NOT NULL,
          price numeric(12,2) NOT NULL CHECK (price > 0),
          compare_at_price numeric(12,2) CHECK (compare_at_price IS NULL OR compare_at_price > price),
          stock_on_hand int NOT NULL DEFAULT 0 CHECK (stock_on_hand >= 0),
          stock_reserved int NOT NULL DEFAULT 0 CHECK (stock_reserved >= 0),
          weight_grams int CHECK (weight_grams IS NULL OR weight_grams > 0),
          barcode text,
          is_active boolean NOT NULL DEFAULT true,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, vendor_id, sku),
          UNIQUE (tenant_id, product_id, option_signature),
          UNIQUE (tenant_id, id),
          CHECK (stock_reserved <= stock_on_hand),
          FOREIGN KEY (tenant_id, product_id) REFERENCES products (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute("CREATE INDEX ix_variants_product ON product_variants (tenant_id, product_id)")
    op.execute(
        """
        CREATE TABLE inventory_movements (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          variant_id uuid NOT NULL,
          delta int NOT NULL CHECK (delta <> 0),
          balance_after int NOT NULL CHECK (balance_after >= 0),
          reason text NOT NULL CHECK (reason IN ('manual','import','order','cancel','return','adjustment')),
          ref text,
          actor_id text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, variant_id) REFERENCES product_variants (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        "CREATE INDEX ix_movements_variant ON inventory_movements (tenant_id, variant_id, created_at DESC)"
    )
    op.execute(
        "CREATE TRIGGER inventory_movements_append_only BEFORE UPDATE OR DELETE ON inventory_movements "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )
    op.execute(
        """
        CREATE TABLE media_assets (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          vendor_id uuid,
          status text NOT NULL DEFAULT 'processing' CHECK (status IN ('processing','ready','failed')),
          source_key text,
          checksum text,
          width int,
          height int,
          renditions jsonb NOT NULL DEFAULT '{}',
          blur_data text,
          alt_text text,
          error text,
          byte_size int,
          created_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_media_checksum ON media_assets (tenant_id, coalesce(vendor_id, tenant_id), checksum) "
        "WHERE checksum IS NOT NULL AND status = 'ready'"
    )
    op.execute("ALTER TABLE media_assets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE media_assets FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY media_assets_isolation ON media_assets USING (tenant_id = app_current_tenant() AND "
        "(app_current_vendor() IS NULL OR vendor_id = app_current_vendor())) WITH CHECK (tenant_id = app_current_tenant() "
        "AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor()))"
    )
    op.execute(
        """
        CREATE TABLE product_media (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          product_id uuid NOT NULL,
          asset_id uuid NOT NULL,
          position int NOT NULL DEFAULT 0,
          UNIQUE (tenant_id, product_id, asset_id),
          FOREIGN KEY (tenant_id, product_id) REFERENCES products (tenant_id, id) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id, asset_id) REFERENCES media_assets (tenant_id, id)
        )"""
    )
    op.execute(
        """
        CREATE TABLE product_questions (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          product_id uuid NOT NULL,
          user_id uuid NOT NULL,
          question text NOT NULL CHECK (length(question) BETWEEN 5 AND 500),
          answer text CHECK (answer IS NULL OR length(answer) BETWEEN 1 AND 1000),
          answered_by text,
          answered_at timestamptz,
          status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','published','hidden')),
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, product_id) REFERENCES products (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        "CREATE INDEX ix_questions_product ON product_questions (tenant_id, product_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE import_jobs (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
          source_key text NOT NULL,
          total_rows int NOT NULL DEFAULT 0,
          succeeded int NOT NULL DEFAULT 0,
          failed int NOT NULL DEFAULT 0,
          errors jsonb NOT NULL DEFAULT '[]',
          created_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          finished_at timestamptz
        )"""
    )
    for t in TENANT_TABLES:
        _rls(t, vendor=False)
    for t in VENDOR_TABLES:
        _rls(t, vendor=True)
    grants = ", ".join(TENANT_TABLES + VENDOR_TABLES + ["media_assets"])
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {grants} TO app, platform; "
        "REVOKE UPDATE, DELETE ON inventory_movements FROM app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    for t in [
        "import_jobs",
        "product_questions",
        "product_media",
        "media_assets",
        "inventory_movements",
        "product_variants",
        "products",
        "brands",
        "category_attributes",
        "categories",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute("ALTER TABLE tenant_settings DROP COLUMN moderation_mode")
