"""discovery: search indexes (tsvector GIN, trigram), attribute GIN, synonyms, wishlists

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_products_search ON products USING gin (search_vector)")
    op.execute(
        "CREATE INDEX ix_products_title_trgm ON products USING gin (lower(title_en) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_products_title_bn_trgm ON products USING gin (title_bn gin_trgm_ops)"
    )
    op.execute("CREATE INDEX ix_products_attrs ON products USING gin (attributes jsonb_path_ops)")
    op.execute(
        "CREATE INDEX ix_products_visible_price ON products (tenant_id, min_price) "
        "WHERE status = 'active' AND moderation_status = 'approved'"
    )
    # RLS hides non-leakproof operators (tsvector @@, jsonb @>) from index scans, which turns every search
    # into a scan of the tenant's rows. These SECURITY DEFINER functions run the search for the *current*
    # tenant only: every table is filtered by app_current_tenant() inside the function (never a parameter).
    # Callers then load the ≤ 24 result rows under RLS by primary key.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION search_candidates(
            p_tsq tsquery, p_category_ids uuid[], p_brand_ids uuid[], p_vendor_ids uuid[],
            p_min numeric, p_max numeric, p_in_stock boolean, p_attrs jsonb)
        RETURNS TABLE (id uuid, rank real, category_id uuid, brand_id uuid, vendor_id uuid,
                       min_price numeric, max_price numeric, attributes jsonb, quality real, in_stock boolean)
        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $fn$
        DECLARE
          q text := 'SELECT p.id, ' ||
            CASE WHEN p_tsq IS NULL THEN '0::real' ELSE 'ts_rank(p.search_vector, $1)' END ||
            ', p.category_id, p.brand_id, p.vendor_id, p.min_price, p.max_price, p.attributes, p.quality_score, p.in_stock
             FROM products p JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id AND v.status = ''approved''
             WHERE p.tenant_id = $9 AND p.status = ''active'' AND p.moderation_status = ''approved''';
        BEGIN
          IF app_current_tenant() IS NULL THEN
            RETURN;
          END IF;
          -- Only predicates that are used, so each call is planned with the GIN indexes.
          IF p_tsq IS NOT NULL THEN q := q || ' AND p.search_vector @@ $1'; END IF;
          IF p_category_ids IS NOT NULL THEN q := q || ' AND p.category_id = ANY($2)'; END IF;
          IF p_brand_ids IS NOT NULL THEN q := q || ' AND p.brand_id = ANY($3)'; END IF;
          IF p_vendor_ids IS NOT NULL THEN q := q || ' AND p.vendor_id = ANY($4)'; END IF;
          IF p_min IS NOT NULL THEN q := q || ' AND p.max_price >= $5'; END IF;
          IF p_max IS NOT NULL THEN q := q || ' AND p.min_price <= $6'; END IF;
          IF p_in_stock THEN q := q || ' AND p.in_stock'; END IF;
          IF p_attrs IS NOT NULL THEN q := q || ' AND p.attributes @> $8'; END IF;
          RETURN QUERY EXECUTE q USING p_tsq, p_category_ids, p_brand_ids, p_vendor_ids, p_min, p_max, p_in_stock,
                                       p_attrs, app_current_tenant();
        END
        $fn$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION search_candidates(tsquery, uuid[], uuid[], uuid[], numeric, numeric, boolean, jsonb) FROM PUBLIC"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT EXECUTE ON FUNCTION search_candidates(tsquery, uuid[], uuid[], uuid[], numeric, numeric, boolean, jsonb) "
        "TO app, platform; END IF; END $$;"
    )
    op.execute(
        """
        CREATE TABLE search_synonyms (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          term text NOT NULL CHECK (length(term) BETWEEN 1 AND 60),
          synonyms text[] NOT NULL CHECK (cardinality(synonyms) BETWEEN 1 AND 20),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, term)
        )"""
    )
    op.execute(
        """
        CREATE TABLE wishlist_items (
          tenant_id uuid NOT NULL,
          user_id uuid NOT NULL,
          product_id uuid NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, user_id, product_id),
          FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id, product_id) REFERENCES products (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    # Distinct words per tenant for typo correction: trigram over a small table, not over every product.
    op.execute(
        """
        CREATE TABLE search_terms (
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          word text NOT NULL CHECK (length(word) BETWEEN 2 AND 40),
          PRIMARY KEY (tenant_id, word)
        )"""
    )
    op.execute("CREATE INDEX ix_search_terms_trgm ON search_terms USING gin (word gin_trgm_ops)")
    for t in ("search_synonyms", "wishlist_items", "search_terms"):
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {t}_isolation ON {t} USING (tenant_id = app_current_tenant()) "
            "WITH CHECK (tenant_id = app_current_tenant())"
        )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON search_synonyms, wishlist_items, search_terms TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS search_candidates(tsquery, uuid[], uuid[], uuid[], numeric, numeric, boolean, jsonb)"
    )
    op.execute("DROP TABLE IF EXISTS search_terms")
    op.execute("DROP TABLE IF EXISTS wishlist_items")
    op.execute("DROP TABLE IF EXISTS search_synonyms")
    for ix in (
        "ix_products_visible_price",
        "ix_products_attrs",
        "ix_products_title_bn_trgm",
        "ix_products_title_trgm",
        "ix_products_search",
    ):
        op.execute(f"DROP INDEX IF EXISTS {ix}")
