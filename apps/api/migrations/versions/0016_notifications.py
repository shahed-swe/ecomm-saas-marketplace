"""notifications, push campaigns, analytics destinations, support tickets

Revision ID: 0016
Revises: 0015
One notification service for every channel (architecture §11); tenant-owned analytics credentials.
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

CHANNELS = "('push','sms','email','in_app')"


def _rls(table: str, vendor: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cond = "tenant_id = app_current_tenant()"
    if vendor:
        cond += " AND (app_current_vendor() IS NULL OR vendor_id IS NULL OR vendor_id = app_current_vendor())"
    op.execute(f"CREATE POLICY {table}_isolation ON {table} USING ({cond}) WITH CHECK ({cond})")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE notification_templates (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          key text NOT NULL,
          channel text NOT NULL CHECK (channel IN """
        + CHANNELS
        + """),
          locale text NOT NULL DEFAULT 'bn' CHECK (locale IN ('bn','en')),
          subject text,
          body text NOT NULL,
          enabled boolean NOT NULL DEFAULT true,
          updated_by text NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, key, channel, locale)
        )"""
    )
    op.execute(
        """
        CREATE TABLE notifications (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          user_id uuid,
          vendor_id uuid,
          staff_id uuid,
          channel text NOT NULL CHECK (channel IN """
        + CHANNELS
        + """),
          key text NOT NULL,
          kind text NOT NULL DEFAULT 'transactional' CHECK (kind IN ('transactional','marketing')),
          locale text NOT NULL DEFAULT 'bn',
          title text,
          body text NOT NULL,
          data jsonb NOT NULL DEFAULT '{}',
          deep_link text,
          status text NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','sent','failed','suppressed','read')),
          dedupe_key text,
          provider_ref text,
          error text,
          created_at timestamptz NOT NULL DEFAULT now(),
          sent_at timestamptz,
          read_at timestamptz
        )"""
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_notification_dedupe ON notifications (tenant_id, dedupe_key) "
        "WHERE dedupe_key IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_notifications_user ON notifications (tenant_id, user_id, created_at DESC) "
        "WHERE user_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE notification_preferences (
          tenant_id uuid NOT NULL,
          user_id uuid NOT NULL,
          marketing_push boolean NOT NULL DEFAULT true,
          marketing_sms boolean NOT NULL DEFAULT false,
          marketing_email boolean NOT NULL DEFAULT true,
          quiet_hours_start int CHECK (quiet_hours_start BETWEEN 0 AND 23),
          quiet_hours_end int CHECK (quiet_hours_end BETWEEN 0 AND 23),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, user_id)
        )"""
    )
    op.execute(
        """
        CREATE TABLE device_tokens (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          user_id uuid NOT NULL,
          platform text NOT NULL CHECK (platform IN ('ios','android','web')),
          app text NOT NULL DEFAULT 'buyer' CHECK (app IN ('buyer','vendor')),
          token text NOT NULL,
          locale text NOT NULL DEFAULT 'bn',
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          revoked_at timestamptz,
          UNIQUE (tenant_id, token)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_device_tokens_user ON device_tokens (tenant_id, user_id) WHERE revoked_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE push_campaigns (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          name text NOT NULL,
          segment text NOT NULL DEFAULT 'all'
            CHECK (segment IN ('all','buyers_with_orders','abandoned_cart','inactive_30d')),
          title text NOT NULL,
          body text NOT NULL,
          deep_link text,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','scheduled','sending','sent','cancelled')),
          scheduled_at timestamptz,
          sent_at timestamptz,
          audience_count int NOT NULL DEFAULT 0,
          sent_count int NOT NULL DEFAULT 0,
          suppressed_count int NOT NULL DEFAULT 0,
          created_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        """
        CREATE TABLE support_tickets (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          number text NOT NULL,
          user_id uuid,
          vendor_id uuid,
          order_id uuid,
          subject text NOT NULL CHECK (length(subject) BETWEEN 3 AND 200),
          category text NOT NULL DEFAULT 'other'
            CHECK (category IN ('order','payment','delivery','return','account','vendor','other')),
          priority text NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high','urgent')),
          status text NOT NULL DEFAULT 'open'
            CHECK (status IN ('open','pending_customer','pending_staff','resolved','closed')),
          assignee_id uuid,
          first_response_at timestamptz,
          resolved_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, number),
          UNIQUE (tenant_id, id)
        )"""
    )
    op.execute(
        """
        CREATE TABLE ticket_messages (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          ticket_id uuid NOT NULL,
          author_kind text NOT NULL CHECK (author_kind IN ('customer','staff','vendor','system')),
          author_id text,
          body text NOT NULL CHECK (length(body) BETWEEN 1 AND 8000),
          internal boolean NOT NULL DEFAULT false,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (tenant_id, ticket_id) REFERENCES support_tickets (tenant_id, id) ON DELETE CASCADE
        )"""
    )
    op.execute(
        "CREATE TRIGGER ticket_messages_append_only BEFORE UPDATE OR DELETE ON ticket_messages "
        "FOR EACH ROW EXECUTE FUNCTION forbid_update_delete()"
    )
    op.execute(
        """
        CREATE TABLE analytics_destinations (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          provider text NOT NULL CHECK (provider IN ('ga4','meta')),
          public_id text,
          secret_ciphertext text,
          server_side boolean NOT NULL DEFAULT false,
          status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
          updated_by text NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, provider)
        )"""
    )
    op.execute(
        """
        CREATE TABLE analytics_events (
          id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
          tenant_id uuid NOT NULL,
          provider text NOT NULL,
          name text NOT NULL,
          ref_type text,
          ref_id uuid,
          payload jsonb NOT NULL DEFAULT '{}',
          status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sent','failed')),
          error text,
          created_at timestamptz NOT NULL DEFAULT now(),
          sent_at timestamptz,
          UNIQUE (tenant_id, provider, ref_type, ref_id, name)
        )"""
    )
    # A person's language belongs to them, not to the store they happen to be shopping in.
    op.execute(
        "ALTER TABLE users ADD COLUMN locale text NOT NULL DEFAULT 'bn' "
        "CHECK (locale IN ('bn','en'))"
    )
    op.execute(
        """
        ALTER TABLE tenant_settings
          ADD COLUMN next_ticket_number int NOT NULL DEFAULT 1,
          ADD COLUMN abandoned_cart_hours int NOT NULL DEFAULT 6 CHECK (abandoned_cart_hours > 0),
          ADD COLUMN marketing_quiet_start int NOT NULL DEFAULT 22 CHECK (marketing_quiet_start BETWEEN 0 AND 23),
          ADD COLUMN marketing_quiet_end int NOT NULL DEFAULT 8 CHECK (marketing_quiet_end BETWEEN 0 AND 23)
        """
    )
    for table in (
        "notification_templates",
        "notifications",
        "notification_preferences",
        "device_tokens",
        "push_campaigns",
        "support_tickets",
        "ticket_messages",
        "analytics_destinations",
        "analytics_events",
    ):
        _rls(table, vendor=table in ("notifications", "support_tickets"))
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app') THEN "
        "GRANT SELECT, INSERT, UPDATE ON notification_templates, notifications, notification_preferences, "
        "device_tokens, push_campaigns, support_tickets, analytics_destinations, analytics_events "
        "TO app, platform; "
        "GRANT SELECT, INSERT ON ticket_messages TO app, platform; "
        "GRANT DELETE ON notification_templates, device_tokens TO app, platform; END IF; END $$;"
    )


def downgrade() -> None:
    for t in (
        "analytics_events",
        "analytics_destinations",
        "ticket_messages",
        "support_tickets",
        "push_campaigns",
        "device_tokens",
        "notification_preferences",
        "notifications",
        "notification_templates",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS locale")
    op.execute(
        "ALTER TABLE tenant_settings DROP COLUMN next_ticket_number, DROP COLUMN abandoned_cart_hours, "
        "DROP COLUMN marketing_quiet_start, DROP COLUMN marketing_quiet_end"
    )
