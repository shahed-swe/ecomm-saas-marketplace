"""baseline: extensions, uuid v7, tenancy session helpers, append-only guard

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # RFC 9562 UUIDv7 generated in the database (defaults for server-side inserts).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION uuid_generate_v7() RETURNS uuid
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE
          ts bytea := substring(int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3);
          b bytea := ts || gen_random_bytes(10);
        BEGIN
          b := set_byte(b, 6, (get_byte(b, 6) & 15) | 112);
          b := set_byte(b, 8, (get_byte(b, 8) & 63) | 128);
          RETURN encode(b, 'hex')::uuid;
        END $$;
        """
    )
    # Tenancy helpers used by every RLS policy (ADR 0001). Missing setting => NULL => no rows.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE AS $$
          SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_vendor() RETURNS uuid
        LANGUAGE sql STABLE AS $$
          SELECT nullif(current_setting('app.vendor_id', true), '')::uuid
        $$;
        """
    )
    # Generic append-only guard for ledgers, audit logs, theme versions.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_update_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS forbid_update_delete()")
    op.execute("DROP FUNCTION IF EXISTS app_current_vendor()")
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant()")
    op.execute("DROP FUNCTION IF EXISTS uuid_generate_v7()")
