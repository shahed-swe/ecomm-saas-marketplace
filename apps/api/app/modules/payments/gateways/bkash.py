"""bKash tokenized checkout: grant token -> create -> (buyer approves) -> execute -> query.

`verify` always calls execute/query on bKash; a callback is never trusted on its own.
Credentials: {app_key, app_secret, username, password}.
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

SANDBOX = "https://tokenized.sandbox.bka.sh/v1.2.0-beta/tokenized"
LIVE = "https://tokenized.pay.bka.sh/v1.2.0-beta/tokenized"
STATUS = {
    "Completed": "paid",
    "Initiated": "pending",
    "Authorized": "pending",
    "Cancelled": "cancelled",
    "Failed": "failed",
}


class BkashGateway:
    name = "bkash"

    def __init__(self, mode: str = "sandbox", client: httpx.AsyncClient | None = None):
        self.base = SANDBOX if mode == "sandbox" else LIVE
        self._client = client

    async def _token(self, c: httpx.AsyncClient, credentials: dict) -> str:
        r = await c.post(
            f"{self.base}/checkout/token/grant",
            json={"app_key": credentials["app_key"], "app_secret": credentials["app_secret"]},
            headers={"username": credentials["username"], "password": credentials["password"]},
        )
        data = r.json()
        if r.status_code != 200 or not data.get("id_token"):
            raise GatewayError(f"bKash token failed: {data.get('statusMessage', r.status_code)}")
        return data["id_token"]

    def _headers(self, token: str, credentials: dict) -> dict:
        return {
            "authorization": token,
            "x-app-key": credentials["app_key"],
            "content-type": "application/json",
        }

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
            token = await self._token(c, credentials)
            r = await c.post(
                f"{self.base}/checkout/create",
                headers=self._headers(token, credentials),
                json={
                    "mode": "0011",
                    "payerReference": buyer_phone or order_number,
                    "callbackURL": callback_url,
                    "amount": f"{amount:.2f}",
                    "currency": "BDT",
                    "intent": "sale",
                    "merchantInvoiceNumber": order_number,
                },
            )
            data = r.json()
            if not data.get("paymentID") or not data.get("bkashURL"):
                raise GatewayError(
                    f"bKash create failed: {data.get('statusMessage', r.status_code)}"
                )
            return CreatedPayment(
                provider_ref=data["paymentID"], redirect_url=data["bkashURL"], raw=data
            )

    async def verify(self, *, credentials, provider_ref: str) -> VerifiedPayment:
        async with self._session() as c:
            token = await self._token(c, credentials)
            r = await c.post(
                f"{self.base}/checkout/execute",
                headers=self._headers(token, credentials),
                json={"paymentID": provider_ref},
            )
            data = r.json()
            if data.get("statusCode") not in ("0000", None) or not data.get("transactionStatus"):
                # already executed or expired: fall back to the authoritative query call
                r = await c.post(
                    f"{self.base}/checkout/payment/status",
                    headers=self._headers(token, credentials),
                    json={"paymentID": provider_ref},
                )
                data = r.json()
            status = STATUS.get(data.get("transactionStatus", ""), "failed")
            amount = Decimal(str(data["amount"])) if data.get("amount") else None
            return VerifiedPayment(
                status=status,
                amount=amount,
                payer_ref=data.get("customerMsisdn"),
                failure_reason=data.get("statusMessage") if status == "failed" else None,
                raw=data,
            )

    def parse_callback(
        self, *, credentials, body: bytes, form: dict, headers: dict
    ) -> CallbackEvent:
        ref = form.get("paymentID") or form.get("paymentId")
        if not ref:
            raise GatewayError("bKash callback without paymentID")
        status = form.get("status", "unknown")
        return CallbackEvent(
            event_id=f"{ref}:{status}", provider_ref=ref, kind=f"bkash.{status}", payload=dict(form)
        )

    async def refund(self, *, credentials, provider_ref: str, amount: Decimal, reason: str) -> dict:
        async with self._session() as c:
            token = await self._token(c, credentials)
            r = await c.post(
                f"{self.base}/checkout/payment/refund",
                headers=self._headers(token, credentials),
                json={
                    "paymentID": provider_ref,
                    "amount": f"{amount:.2f}",
                    "reason": reason[:255],
                    "sku": "order",
                },
            )
            data = r.json()
            if data.get("transactionStatus") not in ("Completed", "Refunded"):
                raise GatewayError(f"bKash refund failed: {data.get('statusMessage')}")
            return data

    async def health(self, *, credentials) -> None:
        async with self._session() as c:
            await self._token(c, credentials)

    @asynccontextmanager
    async def _session(self):
        """An injected client is borrowed, never closed; otherwise one client per call."""
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(timeout=20) as c:
            yield c
