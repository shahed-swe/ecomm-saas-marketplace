"""The chart of accounts (ADR 0003 §7.2). These names are the only ones allowed in the ledger.

Normal balance is stated for each account so a reader can tell at a glance whether a debit is
money coming toward the tenant or going away from it.
"""

GATEWAY_CLEARING = "gateway_clearing:{provider}"  # debit: the gateway owes the tenant
COD_RECEIVABLE = "courier_cod_receivable:{courier}"  # debit: the courier is holding cash
TENANT_BANK = "tenant_bank"  # debit: settled cash
VENDOR_PAYABLE = "vendor_payable"  # credit: owed to a vendor
COMMISSION_REVENUE = "tenant_commission_revenue"  # credit: the tenant's take
GATEWAY_FEE = "gateway_fee_expense"  # debit
COURIER_FEE = "courier_fee_expense"  # debit
SHIPPING_REVENUE = "shipping_fee_revenue"  # credit
PROMO_TENANT = "promo_expense_tenant"  # debit
PROMO_VENDOR = "promo_contra_vendor"  # debit
BUYER_REFUND_PAYABLE = "buyer_refund_payable"  # credit: refunds owed but not yet paid
STORE_CREDIT_LIABILITY = "store_credit_liability"  # credit: wallet balances we owe
VAT_PAYABLE = "vat_output_payable"  # credit: VAT owed to NBR
TDS_PAYABLE = "tds_payable"  # credit: tax deducted from vendor payouts
RESERVE_HOLD = "reserve_hold"  # credit: payable held back during the return window

DEBIT = "debit"
CREDIT = "credit"


def gateway_clearing(provider: str) -> str:
    return GATEWAY_CLEARING.format(provider=provider)


def cod_receivable(courier: str) -> str:
    return COD_RECEIVABLE.format(courier=courier)
