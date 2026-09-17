"""hardening: fixes found by the Phase 20 audit

Revision ID: 0020
Revises: 0019

The isolation audit walks every table and asserts RLS is ENABLED **and FORCED** with a policy.
It found `payment_accounts` enabled but not forced — the table holding tenants' gateway
credentials. Without FORCE, the table's owner reads straight past the policy; the credentials are
encrypted, so this was not an exposure of secrets, but it is exactly the gap the audit exists to
catch, and it is fixed here rather than edited into the original migration so the fix is visible
in the history of a system that handles money.
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE payment_accounts FORCE ROW LEVEL SECURITY")
    # Same belt-and-braces for the newer tables: enabling twice is harmless, missing one is not.
    for table in (
        "app_configs",
        "app_releases",
        "account_deletion_requests",
        "app_store_profiles",
        "app_builds",
        "daily_metrics",
        "report_exports",
    ):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE payment_accounts NO FORCE ROW LEVEL SECURITY")
