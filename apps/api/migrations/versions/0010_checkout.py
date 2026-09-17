"""cart, checkout, order tree, reservations, promotions, shipping, VAT, BD geography

Revision ID: 0010
Revises: 0009
ADR 0003 (ledger timing), 0004 (commission + promotions), 0005 (VAT), 0006 (COD), 0014 (guest via OTP).
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

DIVISIONS = {
    "barishal": (
        "Barishal",
        "বরিশাল",
        ["Barguna", "Barishal", "Bhola", "Jhalokati", "Patuakhali", "Pirojpur"],
    ),
    "chattogram": (
        "Chattogram",
        "চট্টগ্রাম",
        [
            "Bandarban",
            "Brahmanbaria",
            "Chandpur",
            "Chattogram",
            "Cumilla",
            "Cox's Bazar",
            "Feni",
            "Khagrachhari",
            "Lakshmipur",
            "Noakhali",
            "Rangamati",
        ],
    ),
    "dhaka": (
        "Dhaka",
        "ঢাকা",
        [
            "Dhaka",
            "Faridpur",
            "Gazipur",
            "Gopalganj",
            "Kishoreganj",
            "Madaripur",
            "Manikganj",
            "Munshiganj",
            "Narayanganj",
            "Narsingdi",
            "Rajbari",
            "Shariatpur",
            "Tangail",
        ],
    ),
    "khulna": (
        "Khulna",
        "খুলনা",
        [
            "Bagerhat",
            "Chuadanga",
            "Jashore",
            "Jhenaidah",
            "Khulna",
            "Kushtia",
            "Magura",
            "Meherpur",
            "Narail",
            "Satkhira",
        ],
    ),
    "mymensingh": ("Mymensingh", "ময়মনসিংহ", ["Jamalpur", "Mymensingh", "Netrokona", "Sherpur"]),
    "rajshahi": (
        "Rajshahi",
        "রাজশাহী",
        [
            "Bogura",
            "Chapainawabganj",
            "Joypurhat",
            "Naogaon",
            "Natore",
            "Pabna",
            "Rajshahi",
            "Sirajganj",
        ],
    ),
    "rangpur": (
        "Rangpur",
        "রংপুর",
        [
            "Dinajpur",
            "Gaibandha",
            "Kurigram",
            "Lalmonirhat",
            "Nilphamari",
            "Panchagarh",
            "Rangpur",
            "Thakurgaon",
        ],
    ),
    "sylhet": ("Sylhet", "সিলেট", ["Habiganj", "Moulvibazar", "Sunamganj", "Sylhet"]),
}
DHAKA_SUBURB = {"gazipur", "narayanganj", "savar"}

TENANT = [
    "addresses",
    "shipping_rates",
    "coupons",
    "campaigns",
    "campaign_products",
    "tax_rates",
    "carts",
    "cart_items",
    "orders",
    "coupon_redemptions",
]
VENDOR = ["sub_orders", "order_items", "stock_reservations"]


def _slug(name: str) -> str:
    return name.lower().replace("'", "").replace(" ", "-")


def _rls(table: str, vendor: bool) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cond = "tenant_id = app_current_tenant()"
    if vendor:
        cond += " AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor())"
    op.execute(f"CREATE POLICY {table}_isolation ON {table} USING ({cond}) WITH CHECK ({cond})")


def upgrade() -> None:
    # ---------------------------------------------------------------- geography (platform reference data)
    op.execute(
        "CREATE TABLE geo_divisions (code text PRIMARY KEY, name_en text NOT NULL, name_bn text NOT NULL)"
    )
    op.execute(
        """CREATE TABLE geo_districts (code text PRIMARY KEY, division_code text NOT NULL REFERENCES geo_divisions(code),
             name_en text NOT NULL, zone text NOT NULL CHECK (zone IN ('inside_dhaka','dhaka_suburb','outside_dhaka')))"""
    )
    values_div, values_dist = [], []
    for code, (en, bn, districts) in DIVISIONS.items():
        values_div.append(f"('{code}', '{en}', '{bn}')")
        for d in districts:
            s = _slug(d)
            zone = (
                "inside_dhaka"
                if s == "dhaka"
                else "dhaka_suburb"
                if s in DHAKA_SUBURB
                else "outside_dhaka"
            )
            values_dist.append(f"('{s}', '{code}', '{d.replace(chr(39), chr(39) * 2)}', '{zone}')")
    op.execute("INSERT INTO geo_divisions VALUES " + ", ".join(values_div))
    op.execute("INSERT INTO geo_districts VALUES " + ", ".join(values_dist))

    op.execute(
        """
        ALTER TABLE tenant_settings
          ADD COLUMN cod_enabled boolean NOT NULL DEFAULT true,
          ADD COLUMN cod_max_order numeric(12,2) NOT NULL DEFAULT 20000 CHECK (cod_max_order >= 0),
          ADD COLUMN cod_blocked_districts text[] NOT NULL DEFAULT '{}',
          ADD COLUMN vat_registered boolean NOT NULL DEFAULT false,
          ADD COLUMN bin text,
          ADD COLUMN vat_pricing text NOT NULL DEFAULT 'inclusive' CHECK (vat_pricing IN ('inclusive','exclusive')),
          ADD COLUMN default_vat_rate numeric(6,4) NOT NULL DEFAULT 0 CHECK (default_vat_rate >= 0 AND default_vat_rate < 1),
          ADD COLUMN reservation_minutes int NOT NULL DEFAULT 15 CHECK (reservation_minutes BETWEEN 5 AND 120),
          ADD COLUMN order_prefix text NOT NULL DEFAULT 'ORD' CHECK (order_prefix ~ '^[A-Z]{2,5}$'),
          ADD COLUMN next_order_number bigint NOT NULL DEFAULT 1001
        """
    )
    op.execute(
        """
        CREATE TABLE addresses (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          user_id uuid NOT NULL,
          label text CHECK (length(label) <= 30),
          recipient_name text NOT NULL CHECK (length(recipient_name) BETWEEN 2 AND 80),
          phone text NOT NULL CHECK (phone ~ '^\\+8801[3-9][0-9]{8}$'),
          district_code text NOT NULL REFERENCES geo_districts(code),
          upazila text NOT NULL CHECK (length(upazila) BETWEEN 2 AND 60),
          area text CHECK (length(area) <= 80),
          address_line text NOT NULL CHECK (length(address_line) BETWEEN 5 AND 200),
          landmark text CHECK (length(landmark) <= 120),
          is_default boolean NOT NULL DEFAULT false,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_address_default ON addresses (tenant_id, user_id) WHERE is_default"
    )
    op.execute(
        """
        CREATE TABLE shipping_rates (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          vendor_id uuid,
          zone text NOT NULL CHECK (zone IN ('inside_dhaka','dhaka_suburb','outside_dhaka')),
          base_fee numeric(12,2) NOT NULL CHECK (base_fee >= 0),
          base_weight_grams int NOT NULL DEFAULT 1000 CHECK (base_weight_grams > 0),
          per_extra_kg numeric(12,2) NOT NULL DEFAULT 0 CHECK (per_extra_kg >= 0),
          free_over numeric(12,2) CHECK (free_over IS NULL OR free_over > 0),
          free_funded_by text NOT NULL DEFAULT 'vendor' CHECK (free_funded_by IN ('vendor','tenant')),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_shipping_rate ON shipping_rates (tenant_id, coalesce(vendor_id, tenant_id), zone)"
    )
    op.execute(
        """
        CREATE TABLE coupons (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          code citext NOT NULL CHECK (code ~ '^[A-Za-z0-9_-]{3,30}$'),
          kind text NOT NULL CHECK (kind IN ('percent','fixed')),
          value numeric(12,2) NOT NULL CHECK (value > 0),
          min_subtotal numeric(12,2) NOT NULL DEFAULT 0 CHECK (min_subtotal >= 0),
          max_discount numeric(12,2) CHECK (max_discount IS NULL OR max_discount > 0),
          starts_at timestamptz NOT NULL DEFAULT now(),
          ends_at timestamptz,
          usage_limit int CHECK (usage_limit IS NULL OR usage_limit > 0),
          per_buyer_limit int NOT NULL DEFAULT 1 CHECK (per_buyer_limit > 0),
          used_count int NOT NULL DEFAULT 0 CHECK (used_count >= 0),
          is_active boolean NOT NULL DEFAULT true,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, code),
          UNIQUE (tenant_id, id),
          CHECK (kind <> 'percent' OR value <= 90),
          CHECK (usage_limit IS NULL OR used_count <= usage_limit),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        """
        CREATE TABLE campaigns (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          slug citext NOT NULL CHECK (slug ~ '^[a-z0-9-]{2,60}$'),
          name text NOT NULL,
          starts_at timestamptz NOT NULL,
          ends_at timestamptz NOT NULL,
          max_discount_percent numeric(5,2) NOT NULL DEFAULT 90 CHECK (max_discount_percent > 0 AND max_discount_percent <= 90),
          status text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','cancelled')),
          UNIQUE (tenant_id, slug),
          UNIQUE (tenant_id, id),
          CHECK (ends_at > starts_at)
        )"""
    )
    op.execute(
        """
        CREATE TABLE campaign_products (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          campaign_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          variant_id uuid NOT NULL,
          campaign_price numeric(12,2) NOT NULL CHECK (campaign_price > 0),
          stock_cap int NOT NULL CHECK (stock_cap > 0),
          sold_count int NOT NULL DEFAULT 0 CHECK (sold_count >= 0),
          UNIQUE (tenant_id, campaign_id, variant_id),
          CHECK (sold_count <= stock_cap),
          FOREIGN KEY (tenant_id, campaign_id) REFERENCES campaigns (tenant_id, id) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id, variant_id) REFERENCES product_variants (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        """
        CREATE TABLE tax_rates (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          category_id uuid NOT NULL,
          rate numeric(6,4) NOT NULL CHECK (rate >= 0 AND rate < 1),
          UNIQUE (tenant_id, category_id),
          FOREIGN KEY (tenant_id, category_id) REFERENCES categories (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        """
        CREATE TABLE carts (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          user_id uuid,
          token_hash text UNIQUE,
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, id),
          CHECK (user_id IS NOT NULL OR token_hash IS NOT NULL)
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_cart_user ON carts (tenant_id, user_id) WHERE user_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE cart_items (
          tenant_id uuid NOT NULL,
          cart_id uuid NOT NULL,
          variant_id uuid NOT NULL,
          qty int NOT NULL CHECK (qty BETWEEN 1 AND 20),
          added_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, cart_id, variant_id),
          FOREIGN KEY (tenant_id, cart_id) REFERENCES carts (tenant_id, id) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id, variant_id) REFERENCES product_variants (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        """
        CREATE TABLE orders (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          number text NOT NULL,
          user_id uuid,
          contact_phone text NOT NULL,
          contact_email citext,
          payment_method text NOT NULL CHECK (payment_method IN ('cod','bkash','sslcommerz')),
          currency char(3) NOT NULL DEFAULT 'BDT',
          items_subtotal numeric(12,2) NOT NULL,
          discount_total numeric(12,2) NOT NULL DEFAULT 0,
          shipping_total numeric(12,2) NOT NULL DEFAULT 0,
          vat_total numeric(12,2) NOT NULL DEFAULT 0,
          grand_total numeric(12,2) NOT NULL CHECK (grand_total >= 0),
          vat_pricing text NOT NULL,
          shipping_address jsonb NOT NULL,
          idempotency_key text NOT NULL,
          tracking_token_hash text NOT NULL,
          placed_at timestamptz NOT NULL DEFAULT now(),
          payment_due_at timestamptz,
          UNIQUE (tenant_id, number),
          UNIQUE (tenant_id, idempotency_key),
          UNIQUE (tenant_id, id),
          CHECK (grand_total = items_subtotal - discount_total + shipping_total
                 + CASE WHEN vat_pricing = 'exclusive' THEN vat_total ELSE 0 END)
        )"""
    )
    op.execute("CREATE INDEX ix_orders_user ON orders (tenant_id, user_id, placed_at DESC)")
    op.execute(
        """
        CREATE TABLE sub_orders (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          order_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          number text NOT NULL,
          status text NOT NULL CHECK (status IN ('pending_payment','confirmed','processing','ready_to_ship','shipped',
                                                 'delivered','cancelled','returned')),
          items_subtotal numeric(12,2) NOT NULL,
          discount_total numeric(12,2) NOT NULL DEFAULT 0,
          shipping_fee numeric(12,2) NOT NULL DEFAULT 0,
          shipping_waived numeric(12,2) NOT NULL DEFAULT 0,
          shipping_waiver_funded_by text CHECK (shipping_waiver_funded_by IN ('vendor','tenant')),
          vat_total numeric(12,2) NOT NULL DEFAULT 0,
          total numeric(12,2) NOT NULL,
          weight_grams int NOT NULL DEFAULT 0,
          coupon_id uuid,
          commission_rate numeric(6,4) NOT NULL,
          commission_source text NOT NULL CHECK (commission_source IN ('vendor','category','tenant','house')),
          cancelled_at timestamptz,
          cancel_reason text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, number),
          UNIQUE (tenant_id, order_id, vendor_id),
          UNIQUE (tenant_id, id),
          FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_sub_orders_vendor ON sub_orders (tenant_id, vendor_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE order_items (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          sub_order_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          product_id uuid NOT NULL,
          variant_id uuid NOT NULL,
          title_snapshot text NOT NULL,
          sku_snapshot text NOT NULL,
          options_snapshot jsonb NOT NULL DEFAULT '{}',
          list_price numeric(12,2) NOT NULL,
          unit_price numeric(12,2) NOT NULL,
          qty int NOT NULL CHECK (qty > 0),
          discount_amount numeric(12,2) NOT NULL DEFAULT 0,
          vat_rate numeric(6,4) NOT NULL DEFAULT 0,
          vat_amount numeric(12,2) NOT NULL DEFAULT 0,
          line_total numeric(12,2) NOT NULL,
          campaign_product_id uuid,
          FOREIGN KEY (tenant_id, sub_order_id) REFERENCES sub_orders (tenant_id, id),
          CHECK (line_total = unit_price * qty - discount_amount)
        )"""
    )
    op.execute(
        """
        CREATE TABLE stock_reservations (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          order_id uuid NOT NULL,
          sub_order_id uuid NOT NULL,
          variant_id uuid NOT NULL,
          qty int NOT NULL CHECK (qty > 0),
          expires_at timestamptz,
          released_at timestamptz,
          consumed_at timestamptz,
          FOREIGN KEY (tenant_id, variant_id) REFERENCES product_variants (tenant_id, id),
          CHECK (released_at IS NULL OR consumed_at IS NULL)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_reservations_expiry ON stock_reservations (expires_at) "
        "WHERE released_at IS NULL AND consumed_at IS NULL AND expires_at IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE coupon_redemptions (
          tenant_id uuid NOT NULL,
          coupon_id uuid NOT NULL,
          user_id uuid NOT NULL,
          order_id uuid NOT NULL,
          amount numeric(12,2) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, coupon_id, order_id),
          FOREIGN KEY (tenant_id, coupon_id) REFERENCES coupons (tenant_id, id)
        )"""
    )
    # Parent order status is DERIVED from sub-orders, never stored (architecture §6).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION derive_order_status(statuses text[]) RETURNS text
        LANGUAGE sql IMMUTABLE AS $$
          SELECT CASE
            WHEN cardinality(statuses) = 0 THEN 'unknown'
            WHEN statuses <@ ARRAY['cancelled'] THEN 'cancelled'
            WHEN statuses <@ ARRAY['pending_payment','cancelled'] THEN 'pending_payment'
            WHEN statuses <@ ARRAY['delivered','cancelled','returned'] AND 'delivered' = ANY(statuses) THEN
              CASE WHEN statuses <@ ARRAY['delivered'] THEN 'delivered' ELSE 'partially_delivered' END
            WHEN 'shipped' = ANY(statuses) OR 'delivered' = ANY(statuses) THEN
              CASE WHEN statuses <@ ARRAY['shipped','delivered'] THEN 'shipped' ELSE 'partially_shipped' END
            WHEN statuses <@ ARRAY['returned','cancelled'] THEN 'returned'
            ELSE 'processing'
          END
        $$;
        """
    )
    for t in TENANT:
        _rls(t, vendor=False)
    for t in VENDOR:
        _rls(t, vendor=True)
    grants = ", ".join(TENANT + VENDOR)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {grants} TO app, platform; "
        "GRANT SELECT ON geo_divisions, geo_districts TO app, platform; "
        "REVOKE DELETE ON orders, sub_orders, order_items FROM app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS derive_order_status(text[])")
    for t in [
        "coupon_redemptions",
        "stock_reservations",
        "order_items",
        "sub_orders",
        "orders",
        "cart_items",
        "carts",
        "tax_rates",
        "campaign_products",
        "campaigns",
        "coupons",
        "shipping_rates",
        "addresses",
        "geo_districts",
        "geo_divisions",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute(
        "ALTER TABLE tenant_settings DROP COLUMN cod_enabled, DROP COLUMN cod_max_order, DROP COLUMN cod_blocked_districts, "
        "DROP COLUMN vat_registered, DROP COLUMN bin, DROP COLUMN vat_pricing, DROP COLUMN default_vat_rate, "
        "DROP COLUMN reservation_minutes, DROP COLUMN order_prefix, DROP COLUMN next_order_number"
    )
