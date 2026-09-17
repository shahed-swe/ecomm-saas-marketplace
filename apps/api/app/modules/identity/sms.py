"""SmsGateway behind an interface (ADR 0014). Real BD providers plug in later with per-tenant sender IDs."""

from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import log
from app.core.phone import mask_phone


class SmsGateway(Protocol):
    async def send(self, *, tenant_id: str, phone: str, message: str) -> None: ...


class ConsoleSms:
    async def send(self, *, tenant_id: str, phone: str, message: str) -> None:
        log.info("sms_console", tenant_id=tenant_id, phone=mask_phone(phone), length=len(message))


@dataclass
class FakeSms:
    sent: list[tuple[str, str, str]] = field(default_factory=list)

    async def send(self, *, tenant_id: str, phone: str, message: str) -> None:
        self.sent.append((tenant_id, phone, message))

    def last_code(self, phone: str) -> str:
        for _, p, msg in reversed(self.sent):
            if p == phone:
                return "".join(ch for ch in msg if ch.isdigit())[:6]
        raise LookupError(phone)
