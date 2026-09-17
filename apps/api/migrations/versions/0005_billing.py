"""SaaS plans, subscriptions, usage metering, platform invoices

Revision ID: 0005
Revises: 0004
ADR 0011.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE plans (
          code text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]{1,30}$'),
          name text NOT NULL,
          monthly_price numeric(12,2) NOT NULL CHECK (monthly_price >= 0),
          yearly_price numeric(12,2) NOT NULL CHECK (yearly_price >= 0),
          gmv_fee_rate numeric(6,4) NOT NULL DEFAULT 0 CHECK (gmv_fee_rate >= 0 AND gmv_fee_rate < 1),
          setup_fee numeric(12,2) NOT NULL DEFAULT 0 CHECK (setup_fee >= 0),
          trial_days int NOT NULL DEFAULT 14 CHECK (trial_days >= 0),
          limits jsonb NOT NULL DEFAULT '{}',
          is_public boolean NOT NULL DEFAULT true,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        """
        INSERT INTO plans (code, name, monthly_price, yearly_price, gmv_fee_rate, setup_fee, trial_days, limits)
        VALUES
        ('starter', 'Starter', 1500, 15000, 0.0150, 5000, 14,
         '{"vendors": 1, "products": 500, "staff": 3, "custom_domains": 1, "multi_vendor": false,
           "custom_css": false, "white_label_apps": false, "monthly_orders": 1000}'),
        ('growth', 'Growth', 5000, 50000, 0.0100, 15000, 14,
         '{"vendors": 50, "products": 10000, "staff": 10, "custom_domains": 3, "multi_vendor": true,
           "custom_css": true, "white_label_apps": false, "monthly_orders": 20000}'),
        ('scale', 'Scale', 15000, 150000, 0.0075, 40000, 14,
         '{"vendors": 1000, "products": 200000, "staff": 50, "custom_domains": 10, "multi_vendor": true,
           "custom_css": true, "white_label_apps": true, "monthly_orders": 500000}')
        """
    )
    op.execute(
        """
        CREATE TABLE tenant_subscriptions (
          tenant_id uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
          plan_code text NOT NULL REFERENCES plans(code),
          interval text NOT NULL DEFAULT 'monthly' CHECK (interval IN ('monthly','yearly')),
          status text NOT NULL DEFAULT 'trial'
            CHECK (status IN ('trial','active','past_due','suspended','cancelled')),
          trial_ends_at timestamptz,
          current_period_start date NOT NULL,
          current_period_end date NOT NULL,
          setup_fee_invoiced boolean NOT NULL DEFAULT false,
          price_override numeric(12,2),
          gmv_fee_rate_override numeric(6,4),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK (current_period_end > current_period_start)
        )"""
    )
    op.execute(
        """
        CREATE TABLE usage_records (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          metric text NOT NULL CHECK (metric IN ('gmv','orders')),
          period_date date NOT NULL,
          quantity numeric(14,2) NOT NULL,
          source_ref text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, metric, source_ref)
        )"""
    )
    op.execute("CREATE INDEX ix_usage_period ON usage_records (tenant_id, metric, period_date)")
    op.execute("CREATE SEQUENCE platform_invoice_seq")
    op.execute(
        """
        CREATE TABLE platform_invoices (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          number text NOT NULL UNIQUE,
          period_start date NOT NULL,
          period_end date NOT NULL,
          status text NOT NULL DEFAULT 'open' CHECK (status IN ('draft','open','paid','void')),
          currency char(3) NOT NULL DEFAULT 'BDT',
          subtotal numeric(12,2) NOT NULL,
          vat_rate numeric(6,4) NOT NULL DEFAULT 0,
          vat_amount numeric(12,2) NOT NULL DEFAULT 0,
          total numeric(12,2) NOT NULL,
          due_date date NOT NULL,
          paid_at timestamptz,
          payment_method text CHECK (payment_method IN ('bank','bkash','sslcommerz','manual')),
          payment_reference text,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, period_start),
          CHECK (total = subtotal + vat_amount)
        )"""
    )
    op.execute(
        """
        CREATE TABLE platform_invoice_lines (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          invoice_id uuid NOT NULL REFERENCES platform_invoices(id) ON DELETE CASCADE,
          tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          kind text NOT NULL CHECK (kind IN ('subscription','gmv_fee','setup_fee','adjustment')),
          description text NOT NULL,
          quantity numeric(14,2) NOT NULL DEFAULT 1,
          unit_amount numeric(12,4) NOT NULL,
          amount numeric(12,2) NOT NULL
        )"""
    )
    # Tenants may READ their own billing rows; only the platform role writes them.
    for t in (
        "tenant_subscriptions",
        "usage_records",
        "platform_invoices",
        "platform_invoice_lines",
    ):
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {t}_tenant_read ON {t} FOR SELECT USING (tenant_id = app_current_tenant())"
        )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT ON plans, tenant_subscriptions, usage_records, platform_invoices, "
        "platform_invoice_lines TO app; "
        "GRANT SELECT, INSERT, UPDATE ON plans, tenant_subscriptions, usage_records, platform_invoices, "
        "platform_invoice_lines TO platform; "
        "GRANT USAGE ON SEQUENCE platform_invoice_seq TO platform; END IF; END $$;"
    )
    op.execute("ALTER TABLE tenants DROP COLUMN plan_code")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN plan_code text")
    for t in (
        "platform_invoice_lines",
        "platform_invoices",
        "usage_records",
        "tenant_subscriptions",
        "plans",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute("DROP SEQUENCE IF EXISTS platform_invoice_seq")
