"""ledger, vendor balances, payout batches, tax documents

Revision ID: 0014
Revises: 0013
ADR 0003 (chart of accounts, invariants), ADR 0005 (VAT/Mushak), ADR 0009 (payouts, TDS).
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

ENTRY_TYPES = (
    "('capture','cod_delivery','cod_settlement','commission','gateway_fee','courier_fee',"
    "'shipping_fee','discount','refund','return_reversal','reserve_hold','reserve_release',"
    "'dispute_hold','dispute_release','payout','vat','tds','store_credit_issue',"
    "'store_credit_redeem','adjustment')"
)


def _rls(table: str, vendor: bool) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cond = "tenant_id = app_current_tenant()"
    if vendor:
        cond += " AND (app_current_vendor() IS NULL OR vendor_id IS NULL OR vendor_id = app_current_vendor())"
    op.execute(f"CREATE POLICY {table}_isolation ON {table} USING ({cond}) WITH CHECK ({cond})")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ledger_entries (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          group_id uuid NOT NULL,
          account text NOT NULL,
          vendor_id uuid,
          direction text NOT NULL CHECK (direction IN ('debit','credit')),
          amount numeric(12,2) NOT NULL CHECK (amount > 0),
          currency char(3) NOT NULL DEFAULT 'BDT',
          entry_type text NOT NULL CHECK (entry_type IN """
        + ENTRY_TYPES
        + """),
          ref_type text NOT NULL,
          ref_id uuid,
          memo text,
          created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute("CREATE INDEX ix_ledger_account ON ledger_entries (tenant_id, account, created_at)")
    op.execute(
        "CREATE INDEX ix_ledger_vendor ON ledger_entries (tenant_id, vendor_id, created_at) "
        "WHERE vendor_id IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_ledger_group ON ledger_entries (tenant_id, group_id)")
    op.execute("CREATE INDEX ix_ledger_ref ON ledger_entries (tenant_id, ref_type, ref_id)")
    op.execute(
        "CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE OR DELETE ON ledger_entries "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )
    # Invariant 2 (ADR 0003) enforced by the database, not by hope: a group must balance at COMMIT.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledger_group_balanced() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE delta numeric(14,2);
        BEGIN
          SELECT coalesce(sum(CASE WHEN direction = 'debit' THEN amount ELSE -amount END), 0)
            INTO delta FROM ledger_entries
            WHERE tenant_id = NEW.tenant_id AND group_id = NEW.group_id;
          IF delta <> 0 THEN
            RAISE EXCEPTION 'ledger group % does not balance (off by %)', NEW.group_id, delta
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NULL;
        END $$;
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER ledger_group_balanced AFTER INSERT ON ledger_entries "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger_group_balanced()"
    )
    # One posting per business event: a capture cannot be booked twice.
    op.execute(
        "CREATE UNIQUE INDEX uq_ledger_event ON ledger_entries (tenant_id, entry_type, ref_type, ref_id, account, vendor_id) "
        "WHERE ref_id IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE payout_batches (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          period_end date NOT NULL,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','approved','exported','completed','cancelled')),
          gross_total numeric(12,2) NOT NULL DEFAULT 0,
          tds_total numeric(12,2) NOT NULL DEFAULT 0,
          net_total numeric(12,2) NOT NULL DEFAULT 0,
          line_count int NOT NULL DEFAULT 0,
          created_by text NOT NULL,
          approved_by text,
          approved_at timestamptz,
          exported_at timestamptz,
          completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, period_end),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        """
        CREATE TABLE payout_lines (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          batch_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          period_end date NOT NULL,
          gross numeric(12,2) NOT NULL CHECK (gross > 0),
          tds numeric(12,2) NOT NULL DEFAULT 0 CHECK (tds >= 0),
          net numeric(12,2) NOT NULL CHECK (net > 0),
          method text NOT NULL CHECK (method IN ('bank','bkash')),
          account_last4 text,
          account_name text,
          status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','failed','cancelled')),
          reference text,
          failure_reason text,
          paid_at timestamptz,
          UNIQUE (tenant_id, vendor_id, period_end),
          FOREIGN KEY (tenant_id, batch_id) REFERENCES payout_batches (tenant_id, id) ON DELETE CASCADE,
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id)
        )"""
    )
    op.execute("CREATE INDEX ix_payout_lines_batch ON payout_lines (tenant_id, batch_id)")
    op.execute(
        """
        CREATE TABLE tax_invoices (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          number text NOT NULL,
          sub_order_id uuid NOT NULL,
          kind text NOT NULL DEFAULT 'invoice' CHECK (kind IN ('invoice','credit_note')),
          taxable_amount numeric(12,2) NOT NULL CHECK (taxable_amount >= 0),
          vat_amount numeric(12,2) NOT NULL DEFAULT 0 CHECK (vat_amount >= 0),
          seller_bin text,
          document_key text,
          issued_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, number),
          UNIQUE (tenant_id, sub_order_id, kind),
          FOREIGN KEY (tenant_id, sub_order_id) REFERENCES sub_orders (tenant_id, id)
        )"""
    )
    op.execute(
        """
        ALTER TABLE tenant_settings
          ADD COLUMN payout_schedule text NOT NULL DEFAULT 'weekly'
            CHECK (payout_schedule IN ('weekly','biweekly','monthly')),
          ADD COLUMN payout_min_amount numeric(12,2) NOT NULL DEFAULT 500 CHECK (payout_min_amount >= 0),
          ADD COLUMN reserve_mode text NOT NULL DEFAULT 'window'
            CHECK (reserve_mode IN ('none','window','percent')),
          ADD COLUMN reserve_percent numeric(6,4) NOT NULL DEFAULT 0 CHECK (reserve_percent >= 0 AND reserve_percent < 1),
          ADD COLUMN reserve_days int NOT NULL DEFAULT 7 CHECK (reserve_days >= 0),
          ADD COLUMN tds_rate numeric(6,4) NOT NULL DEFAULT 0 CHECK (tds_rate >= 0 AND tds_rate < 1),
          ADD COLUMN vat_bin text,
          ADD COLUMN maker_checker boolean NOT NULL DEFAULT true,
          ADD COLUMN next_invoice_number int NOT NULL DEFAULT 1
        """
    )
    for table in ("ledger_entries", "payout_batches", "payout_lines", "tax_invoices"):
        _rls(table, vendor=table in ("ledger_entries", "payout_lines", "tax_invoices"))
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT ON ledger_entries TO app, platform; "
        "GRANT SELECT, INSERT, UPDATE ON payout_batches, payout_lines, tax_invoices TO app, platform; "
        "GRANT DELETE ON payout_lines TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    for t in ("tax_invoices", "payout_lines", "payout_batches", "ledger_entries"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS ledger_group_balanced()")
    op.execute(
        "ALTER TABLE tenant_settings DROP COLUMN payout_schedule, DROP COLUMN payout_min_amount, "
        "DROP COLUMN reserve_mode, DROP COLUMN reserve_percent, DROP COLUMN reserve_days, "
        "DROP COLUMN tds_rate, DROP COLUMN vat_bin, DROP COLUMN maker_checker, "
        "DROP COLUMN next_invoice_number"
    )
