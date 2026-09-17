"""SSLCommerz hosted checkout: session init -> hosted page -> IPN -> validation API.

The IPN is only a nudge: `verify` calls `validationserverAPI` with the val_id before anything is paid.
Credentials: {store_id, store_passwd}.
"""

from contextlib import asynccontextmanager
from decimal import Decimal

import httpx

from app.modules.payments.gateways.base import (
    CallbackEvent,
    CreatedPayment,
    GatewayError,
    VerifiedPayment,
)

SANDBOX = "https://sandbox.sslcommerz.com"
LIVE = "https://securepay.sslcommerz.com"
STATUS = {
    "VALID": "paid",
    "VALIDATED": "paid",
    "PENDING": "pending",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "EXPIRED": "failed",
    "UNATTEMPTED": "pending",
}


class SslCommerzGateway:
    name = "sslcommerz"

    def __init__(self, mode: str = "sandbox", client: httpx.AsyncClient | None = None):
        self.base = SANDBOX if mode == "sandbox" else LIVE
        self._client = client

    @asynccontextmanager
    async def _session(self):
        """An injected client is borrowed, never closed; otherwise one client per call."""
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(timeout=20) as c:
            yield c

    async def create(
        self,
        *,
        credentials,
        amount: Decimal,
        order_number: str,
        return_url: str,
        callback_url: str,
        buyer_phone: str | None,
    ) -> CreatedPayment:
        async with self._session() as c:
            r = await c.post(
                f"{self.base}/gwprocess/v4/api.php",
                data={
                    "store_id": credentials["store_id"],
                    "store_passwd": credentials["store_passwd"],
                    "total_amount": f"{amount:.2f}",
                    "currency": "BDT",
                    "tran_id": order_number,
                    "success_url": return_url,
                    "fail_url": return_url,
                    "cancel_url": return_url,
                    "ipn_url": callback_url,
                    "cus_name": "Customer",
                    "cus_phone": buyer_phone or "01700000000",
                    "cus_email": "buyer@example.com",
                    "shipping_method": "Courier",
                    "product_name": "Order",
                    "product_category": "General",
                    "product_profile": "general",
                    "num_of_item": 1,
                },
            )
            data = r.json()
            if data.get("status") != "SUCCESS" or not data.get("GatewayPageURL"):
                raise GatewayError(
                    f"SSLCommerz init failed: {data.get('failedreason', r.status_code)}"
                )
            return CreatedPayment(
                provider_ref=data.get("sessionkey", order_number),
                redirect_url=data["GatewayPageURL"],
                raw=data,
            )

    async def verify(self, *, credentials, provider_ref: str) -> VerifiedPayment:
        """provider_ref carries the val_id once the IPN arrives; before that the session key has no result yet."""
        if not provider_ref.startswith("val:"):
            return VerifiedPayment(status="pending")
        async with self._session() as c:
            r = await c.get(
                f"{self.base}/validator/api/validationserverAPI.php",
                params={
                    "val_id": provider_ref.removeprefix("val:"),
                    "store_id": credentials["store_id"],
                    "store_passwd": credentials["store_passwd"],
                    "format": "json",
                },
            )
            data = r.json()
            status = STATUS.get(str(data.get("status", "")).upper(), "failed")
            amount = Decimal(str(data["amount"])) if data.get("amount") else None
            fee = Decimal(
                str(
                    data.get("store_amount")
                    and Decimal(str(data["amount"])) - Decimal(str(data["store_amount"]))
                    or 0
                )
            )
            return VerifiedPayment(
                status=status,
                amount=amount,
                payer_ref=data.get("card_no"),
                fee=fee,
                failure_reason=data.get("error") if status == "failed" else None,
                raw=data,
            )

    def parse_callback(
        self, *, credentials, body: bytes, form: dict, headers: dict
    ) -> CallbackEvent:
        val_id, tran_id = form.get("val_id"), form.get("tran_id")
        if not val_id or not tran_id:
            raise GatewayError("SSLCommerz IPN without val_id")
        return CallbackEvent(
            event_id=val_id,
            provider_ref=f"val:{val_id}",
            kind="sslcommerz.ipn",
            payload={**dict(form), "tran_id": tran_id},
        )

    async def refund(self, *, credentials, provider_ref: str, amount: Decimal, reason: str) -> dict:
        async with self._session() as c:
            r = await c.get(
                f"{self.base}/validator/api/merchantTransIDvalidationAPI.php",
                params={
                    "bank_tran_id": provider_ref.removeprefix("val:"),
                    "refund_amount": f"{amount:.2f}",
                    "refund_remarks": reason[:255],
                    "store_id": credentials["store_id"],
                    "store_passwd": credentials["store_passwd"],
                    "format": "json",
                },
            )
            data = r.json()
            if str(data.get("status", "")).lower() not in ("success", "processing"):
                raise GatewayError(f"SSLCommerz refund failed: {data.get('errorReason')}")
            return data

    async def health(self, *, credentials) -> None:
        async with self._session() as c:
            r = await c.post(
                f"{self.base}/gwprocess/v4/api.php",
                data={
                    "store_id": credentials["store_id"],
                    "store_passwd": credentials["store_passwd"],
                    "total_amount": "10.00",
                    "currency": "BDT",
                    "tran_id": "healthcheck",
                    "success_url": "https://example.com",
                    "fail_url": "https://example.com",
                    "cancel_url": "https://example.com",
                    "cus_name": "x",
                    "cus_phone": "01700000000",
                    "cus_email": "x@example.com",
                    "shipping_method": "NO",
                    "product_name": "x",
                    "product_category": "x",
                    "product_profile": "general",
                    "num_of_item": 1,
                },
            )
            if r.json().get("status") == "FAILED":
                raise GatewayError(r.json().get("failedreason", "credentials rejected"))
