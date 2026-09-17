"""Permission catalogue and system roles (ADR 0014). Roles are bundles; checks use permissions."""

PERMISSIONS: dict[str, str] = {
    "settings.read": "View store settings and domains",
    "settings.write": "Change store settings and domains",
    "staff.manage": "Invite staff and change roles",
    "audit.read": "View audit log",
    "vendors.read": "View vendors",
    "vendors.write": "Create, approve, suspend vendors",
    "catalog.read": "View products",
    "catalog.write": "Edit products and taxonomy",
    "catalog.moderate": "Approve or reject listings",
    "orders.read": "View orders",
    "orders.write": "Update orders, cancellations",
    "returns.manage": "Handle returns",
    "refunds.approve": "Approve refunds",
    "payouts.read": "View payouts",
    "payouts.prepare": "Prepare payout batches",
    "payouts.approve": "Approve payout batches",
    "finance.read": "View ledger and reports",
    "theme.edit": "Edit theme drafts",
    "theme.publish": "Publish themes",
    "content.write": "Edit pages and menus",
    "marketing.write": "Campaigns, coupons, notifications",
    "support.tickets": "Handle support tickets",
    "customers.read": "View customers",
    "reports.read": "View analytics",
}

ALL = "*"

SYSTEM_ROLES: dict[str, tuple[str, list[str]]] = {
    "owner": ("Owner", [ALL]),
    "finance": (
        "Finance",
        [
            "finance.read",
            "payouts.read",
            "payouts.prepare",
            "payouts.approve",
            "refunds.approve",
            "orders.read",
            "reports.read",
        ],
    ),
    "support": (
        "Support",
        [
            "orders.read",
            "orders.write",
            "returns.manage",
            "support.tickets",
            "customers.read",
            "vendors.read",
        ],
    ),
    "moderator": (
        "Moderator",
        ["catalog.read", "catalog.moderate", "vendors.read", "vendors.write"],
    ),
    "content": (
        "Content",
        [
            "theme.edit",
            "theme.publish",
            "content.write",
            "catalog.read",
            "marketing.write",
            "settings.read",
        ],
    ),
}

VENDOR_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {"*"},
    "manager": {
        "storefront.write",
        "catalog.write",
        "orders.write",
        "staff.manage",
        "finance.read",
    },
    "staff": {"catalog.write", "orders.write"},
}


def has_permission(granted: list[str] | set[str], needed: str) -> bool:
    return ALL in granted or needed in granted
