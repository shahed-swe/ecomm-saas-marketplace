"""trust & safety: verified reviews, disputes with holds, vendor scoring, buyer-vendor messaging

Revision ID: 0015
Revises: 0014
ADR 0008 (returns interplay), ADR 0009 (payout holds), architecture §13 (trust).
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

DISPUTE_STATUSES = (
    "('open','needs_buyer','needs_vendor','under_review','resolved_buyer','resolved_vendor',"
    "'withdrawn','cancelled')"
)


def _rls(table: str, vendor: bool) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cond = "tenant_id = app_current_tenant()"
    if vendor:
        cond += " AND (app_current_vendor() IS NULL OR vendor_id = app_current_vendor())"
    op.execute(f"CREATE POLICY {table}_isolation ON {table} USING ({cond}) WITH CHECK ({cond})")


def upgrade() -> None:
    # ------------------------------------------------------------------ reviews (verified only)
    op.execute(
        """
        CREATE TABLE reviews (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          product_id uuid NOT NULL,
          user_id uuid NOT NULL,
          order_item_id uuid NOT NULL,
          rating int NOT NULL CHECK (rating BETWEEN 1 AND 5),
          title text CHECK (title IS NULL OR length(title) <= 120),
          body text CHECK (body IS NULL OR length(body) <= 2000),
          status text NOT NULL DEFAULT 'published'
            CHECK (status IN ('pending','published','rejected','hidden')),
          flagged_reasons text[] NOT NULL DEFAULT '{}',
          helpful_count int NOT NULL DEFAULT 0 CHECK (helpful_count >= 0),
          vendor_reply text CHECK (vendor_reply IS NULL OR length(vendor_reply) <= 1000),
          replied_at timestamptz,
          moderated_by text,
          moderated_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, order_item_id),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_reviews_product ON reviews (tenant_id, product_id, created_at DESC) "
        "WHERE status = 'published'"
    )
    op.execute(
        """
        CREATE TABLE review_votes (
          tenant_id uuid NOT NULL,
          review_id uuid NOT NULL,
          user_id uuid NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, review_id, user_id),
          FOREIGN KEY (tenant_id, review_id) REFERENCES reviews (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        """ALTER TABLE products
             ADD COLUMN rating_avg numeric(3,2) NOT NULL DEFAULT 0 CHECK (rating_avg >= 0 AND rating_avg <= 5),
             ADD COLUMN rating_count int NOT NULL DEFAULT 0 CHECK (rating_count >= 0)"""
    )
    op.execute(
        """ALTER TABLE vendors
             ADD COLUMN rating_avg numeric(3,2) NOT NULL DEFAULT 0 CHECK (rating_avg >= 0 AND rating_avg <= 5),
             ADD COLUMN rating_count int NOT NULL DEFAULT 0 CHECK (rating_count >= 0)"""
    )

    # ------------------------------------------------------------------------------- disputes
    op.execute(
        """
        CREATE TABLE disputes (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          order_id uuid NOT NULL,
          sub_order_id uuid NOT NULL,
          user_id uuid NOT NULL,
          number text NOT NULL,
          reason text NOT NULL CHECK (reason IN ('not_received','not_as_described','damaged','refund_not_received','other')),
          detail text,
          amount numeric(12,2) NOT NULL CHECK (amount >= 0),
          status text NOT NULL DEFAULT 'open' CHECK (status IN """
        + DISPUTE_STATUSES
        + """),
          hold_id uuid,
          due_at timestamptz,
          resolution text,
          resolved_by text,
          resolved_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, number),
          UNIQUE (tenant_id, id),
          FOREIGN KEY (tenant_id, sub_order_id) REFERENCES sub_orders (tenant_id, id)
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_dispute_open_per_shipment ON disputes (tenant_id, sub_order_id) "
        "WHERE status NOT IN ('resolved_buyer','resolved_vendor','withdrawn','cancelled')"
    )
    op.execute(
        """
        CREATE TABLE dispute_messages (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          dispute_id uuid NOT NULL,
          author_kind text NOT NULL CHECK (author_kind IN ('buyer','vendor','staff')),
          author_id text NOT NULL,
          body text NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, dispute_id) REFERENCES disputes (tenant_id, id) ON DELETE CASCADE
        )"""
    )

    # --------------------------------------------------------------------- buyer-vendor messaging
    op.execute(
        """
        CREATE TABLE conversations (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          user_id uuid NOT NULL,
          sub_order_id uuid,
          status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','blocked')),
          last_message_at timestamptz,
          buyer_unread int NOT NULL DEFAULT 0 CHECK (buyer_unread >= 0),
          vendor_unread int NOT NULL DEFAULT 0 CHECK (vendor_unread >= 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, id),
          UNIQUE (tenant_id, vendor_id, user_id, sub_order_id),
          FOREIGN KEY (tenant_id, vendor_id) REFERENCES vendors (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        """
        CREATE TABLE messages (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          conversation_id uuid NOT NULL,
          sender_kind text NOT NULL CHECK (sender_kind IN ('buyer','vendor','staff')),
          sender_id uuid,
          body text NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
          original_hash text,
          redacted boolean NOT NULL DEFAULT false,
          flags text[] NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        "CREATE INDEX ix_messages_conversation ON messages (tenant_id, conversation_id, created_at)"
    )
    op.execute(
        "CREATE TRIGGER messages_append_only BEFORE UPDATE OR DELETE ON messages "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )

    # ------------------------------------------------------------------------- vendor scorecard
    op.execute(
        """
        CREATE TABLE vendor_scores (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          vendor_id uuid NOT NULL,
          window_days int NOT NULL,
          orders int NOT NULL DEFAULT 0,
          on_time_rate numeric(5,4),
          cancel_rate numeric(5,4),
          return_rate numeric(5,4),
          dispute_rate numeric(5,4),
          rating_avg numeric(3,2),
          score numeric(5,2) NOT NULL,
          band text NOT NULL CHECK (band IN ('good','watch','risk')),
          computed_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, vendor_id, window_days)
        )"""
    )
    op.execute(
        """
        ALTER TABLE tenant_settings
          ADD COLUMN review_mode text NOT NULL DEFAULT 'auto' CHECK (review_mode IN ('auto','manual')),
          ADD COLUMN review_window_days int NOT NULL DEFAULT 60 CHECK (review_window_days > 0),
          ADD COLUMN messaging_enabled boolean NOT NULL DEFAULT true,
          ADD COLUMN dispute_response_hours int NOT NULL DEFAULT 48 CHECK (dispute_response_hours > 0),
          ADD COLUMN next_dispute_number int NOT NULL DEFAULT 1
        """
    )
    for table in ("reviews", "disputes", "conversations", "messages", "vendor_scores"):
        _rls(table, vendor=True)
    for table in ("review_votes", "dispute_messages"):
        _rls(table, vendor=False)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON reviews, disputes, conversations, vendor_scores TO app, platform; "
        "GRANT SELECT, INSERT ON messages, dispute_messages, review_votes TO app, platform; "
        "GRANT DELETE ON review_votes TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    for t in (
        "vendor_scores",
        "messages",
        "conversations",
        "dispute_messages",
        "disputes",
        "review_votes",
        "reviews",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute(
        "ALTER TABLE products DROP COLUMN IF EXISTS rating_avg, DROP COLUMN IF EXISTS rating_count"
    )
    op.execute(
        "ALTER TABLE vendors DROP COLUMN IF EXISTS rating_avg, DROP COLUMN IF EXISTS rating_count"
    )
    op.execute(
        "ALTER TABLE tenant_settings DROP COLUMN review_mode, DROP COLUMN review_window_days, "
        "DROP COLUMN messaging_enabled, DROP COLUMN dispute_response_hours, "
        "DROP COLUMN next_dispute_number"
    )
