"""returns, refunds, store credit, credit notes

Revision ID: 0013
Revises: 0012
ADR 0008 (return flow, refund rails, store credit), ADR 0005 (VAT on credit notes).
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

RETURN_STATUSES = (
    "('requested','approved','rejected','pickup_booked','picked_up','received',"
    "'qc_passed','qc_failed','refunded','returned_to_buyer','cancelled')"
)
REASONS = "('damaged','wrong_item','not_as_described','missing_parts','size_issue','changed_mind','other')"


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
          ADD COLUMN return_window_days int NOT NULL DEFAULT 7 CHECK (return_window_days BETWEEN 0 AND 60),
          ADD COLUMN return_auto_approve_reasons text[] NOT NULL DEFAULT '{}',
          ADD COLUMN qc_sla_hours int NOT NULL DEFAULT 48 CHECK (qc_sla_hours > 0),
          ADD COLUMN store_credit_enabled boolean NOT NULL DEFAULT true,
          ADD COLUMN store_credit_expiry_days int CHECK (store_credit_expiry_days > 0)
        """
    )
    # A category may shorten or extend the tenant's window (perishables vs electronics).
    op.execute(
        "ALTER TABLE categories ADD COLUMN return_window_days int CHECK (return_window_days BETWEEN 0 AND 60)"
    )

    op.execute(
        """
        CREATE TABLE return_requests (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          order_id uuid NOT NULL,
          sub_order_id uuid NOT NULL,
          user_id uuid NOT NULL,
          number text NOT NULL,
          reason text NOT NULL CHECK (reason IN """
        + REASONS
        + """),
          reason_note text,
          status text NOT NULL DEFAULT 'requested' CHECK (status IN """
        + RETURN_STATUSES
        + """),
          shipping_payer text NOT NULL CHECK (shipping_payer IN ('vendor','buyer')),
          return_shipping_fee numeric(12,2) NOT NULL DEFAULT 0 CHECK (return_shipping_fee >= 0),
          refund_method text NOT NULL DEFAULT 'original'
            CHECK (refund_method IN ('original','store_credit','bkash','bank')),
          refund_target_ciphertext text,
          refund_total numeric(12,2) NOT NULL DEFAULT 0 CHECK (refund_total >= 0),
          pickup_courier text,
          pickup_consignment_id text,
          pickup_tracking_url text,
          decided_by text,
          decided_at timestamptz,
          decision_note text,
          qc_by text,
          qc_at timestamptz,
          qc_note text,
          qc_due_at timestamptz,
          escalated boolean NOT NULL DEFAULT false,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, number),
          UNIQUE (tenant_id, id),
          FOREIGN KEY (tenant_id, sub_order_id) REFERENCES sub_orders (tenant_id, id),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_returns_vendor ON return_requests (tenant_id, vendor_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE return_items (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          return_id uuid NOT NULL,
          order_item_id uuid NOT NULL,
          variant_id uuid NOT NULL,
          qty int NOT NULL CHECK (qty > 0),
          refund_amount numeric(12,2) NOT NULL CHECK (refund_amount >= 0),
          vat_amount numeric(12,2) NOT NULL DEFAULT 0 CHECK (vat_amount >= 0),
          qc_result text CHECK (qc_result IS NULL OR qc_result IN ('restock','write_off','reject')),
          UNIQUE (tenant_id, return_id, order_item_id),
          FOREIGN KEY (tenant_id, return_id) REFERENCES return_requests (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        """
        CREATE TABLE return_photos (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          return_id uuid NOT NULL,
          object_key text NOT NULL,
          content_type text NOT NULL,
          uploaded_by text NOT NULL,
          uploaded_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, return_id) REFERENCES return_requests (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        """
        CREATE TABLE refunds (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          return_id uuid,
          order_id uuid NOT NULL,
          payment_id uuid,
          method text NOT NULL CHECK (method IN ('bkash','sslcommerz','store_credit','manual_bkash','manual_bank')),
          amount numeric(12,2) NOT NULL CHECK (amount > 0),
          status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','processing','completed','failed','cancelled')),
          provider_ref text,
          failure_reason text,
          requested_by text NOT NULL,
          completed_by text,
          created_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_refund_open_per_return ON refunds (tenant_id, return_id) "
        "WHERE status IN ('pending','processing','completed')"
    )
    op.execute(
        """
        CREATE TABLE store_credit_accounts (
          tenant_id uuid NOT NULL,
          user_id uuid NOT NULL,
          balance numeric(12,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, user_id)
        )"""
    )
    op.execute(
        """
        CREATE TABLE store_credit_entries (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          user_id uuid NOT NULL,
          delta numeric(12,2) NOT NULL CHECK (delta <> 0),
          balance_after numeric(12,2) NOT NULL CHECK (balance_after >= 0),
          reason text NOT NULL CHECK (reason IN ('return_refund','goodwill','order_payment','order_cancelled','expiry','adjustment')),
          ref_type text,
          ref_id uuid,
          note text,
          actor_id text NOT NULL,
          expires_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE TRIGGER store_credit_entries_append_only BEFORE UPDATE OR DELETE ON store_credit_entries "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )
    op.execute(
        "CREATE INDEX ix_store_credit_entries_user ON store_credit_entries (tenant_id, user_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE credit_notes (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          number text NOT NULL,
          return_id uuid,
          sub_order_id uuid NOT NULL,
          amount numeric(12,2) NOT NULL CHECK (amount >= 0),
          vat_amount numeric(12,2) NOT NULL DEFAULT 0 CHECK (vat_amount >= 0),
          reason text NOT NULL,
          document_key text,
          issued_by text NOT NULL,
          issued_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, number),
          FOREIGN KEY (tenant_id, sub_order_id) REFERENCES sub_orders (tenant_id, id)
        )"""
    )
    op.execute(
        "ALTER TABLE tenant_settings ADD COLUMN next_credit_note_number int NOT NULL DEFAULT 1, "
        "ADD COLUMN next_return_number int NOT NULL DEFAULT 1"
    )
    # Store credit is tender, not a discount: it is a payment on the order like any other.
    op.execute("ALTER TABLE payments DROP CONSTRAINT payments_provider_check")
    op.execute(
        "ALTER TABLE payments ADD CONSTRAINT payments_provider_check "
        "CHECK (provider IN ('bkash','sslcommerz','cod','store_credit'))"
    )
    # ...so "one paid payment per order" becomes "one paid *gateway* payment per order":
    op.execute("DROP INDEX IF EXISTS uq_payment_paid_per_order")
    op.execute(
        "CREATE UNIQUE INDEX uq_payment_paid_per_order ON payments (tenant_id, order_id) "
        "WHERE status IN ('paid','refunded','partially_refunded') AND provider <> 'store_credit'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_store_credit_payment_per_order ON payments (tenant_id, order_id) "
        "WHERE provider = 'store_credit' AND status <> 'cancelled'"
    )
    op.execute(
        "ALTER TABLE payments ADD COLUMN refunded_amount numeric(12,2) NOT NULL DEFAULT 0 "
        "CHECK (refunded_amount >= 0)"
    )

    for table in (
        "return_requests",
        "return_items",
        "return_photos",
        "refunds",
        "store_credit_accounts",
        "store_credit_entries",
        "credit_notes",
    ):
        _rls(table, vendor=table in ("return_requests", "credit_notes"))
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON return_requests, return_items, return_photos, refunds, "
        "store_credit_accounts, credit_notes TO app, platform; "
        "GRANT SELECT, INSERT ON store_credit_entries TO app, platform; "
        "GRANT DELETE ON return_photos TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_store_credit_payment_per_order")
    op.execute("DROP INDEX IF EXISTS uq_payment_paid_per_order")
    op.execute(
        "CREATE UNIQUE INDEX uq_payment_paid_per_order ON payments (tenant_id, order_id) "
        "WHERE status IN ('paid','refunded','partially_refunded')"
    )
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS refunded_amount")
    op.execute("ALTER TABLE payments DROP CONSTRAINT payments_provider_check")
    op.execute(
        "ALTER TABLE payments ADD CONSTRAINT payments_provider_check "
        "CHECK (provider IN ('bkash','sslcommerz','cod'))"
    )
    for t in (
        "credit_notes",
        "store_credit_entries",
        "store_credit_accounts",
        "refunds",
        "return_photos",
        "return_items",
        "return_requests",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute("ALTER TABLE categories DROP COLUMN IF EXISTS return_window_days")
    op.execute(
        "ALTER TABLE tenant_settings DROP COLUMN return_window_days, DROP COLUMN return_auto_approve_reasons, "
        "DROP COLUMN qc_sla_hours, DROP COLUMN store_credit_enabled, DROP COLUMN store_credit_expiry_days, "
        "DROP COLUMN next_credit_note_number, DROP COLUMN next_return_number"
    )
