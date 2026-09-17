"""Channel adapters. Each one does exactly one thing: put a rendered message somewhere.

Delivery is best-effort and never blocks the transaction that caused it: a failed SMS must not
roll back a paid order. Failures are recorded on the notification row instead.
"""

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.core.logging import log


class ChannelError(Exception):
    pass


class PushSender(Protocol):
    async def send(
        self, *, tenant_id: str, tokens: list[str], title: str, body: str, data: dict
    ) -> list[str]: ...


class EmailSender(Protocol):
    async def send(self, *, tenant_id: str, to: str, subject: str, body: str) -> str | None: ...


class ConsolePush:
    """Development and tests: logs instead of calling FCM."""

    async def send(self, *, tenant_id, tokens, title, body, data) -> list[str]:
        log.info("push_console", tenant_id=tenant_id, tokens=len(tokens), title=title)
        return list(tokens)


@dataclass
class FakePush:
    sent: list[dict] = field(default_factory=list)

    async def send(self, *, tenant_id, tokens, title, body, data) -> list[str]:
        self.sent.append(
            {
                "tenant_id": tenant_id,
                "tokens": list(tokens),
                "title": title,
                "body": body,
                "data": data,
            }
        )
        return list(tokens)


class FcmPush:
    """Firebase HTTP v1. One project per platform tenant; the tenant's app carries its own sender id."""

    def __init__(self, project_id: str, access_token: str, client: httpx.AsyncClient | None = None):
        self.project_id = project_id
        self.access_token = access_token
        self._client = client

    async def send(self, *, tenant_id, tokens, title, body, data) -> list[str]:
        delivered = []
        async with self._client or httpx.AsyncClient(timeout=15) as c:
            for token in tokens:
                r = await c.post(
                    f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
                    headers={"authorization": f"Bearer {self.access_token}"},
                    json={
                        "message": {
                            "token": token,
                            "notification": {"title": title, "body": body},
                            "data": {k: str(v) for k, v in (data or {}).items()},
                        }
                    },
                )
                if r.status_code < 300:
                    delivered.append(token)
        return delivered


class ConsoleEmail:
    async def send(self, *, tenant_id, to, subject, body) -> str | None:
        log.info("email_console", tenant_id=tenant_id, to=to.split("@")[-1], subject=subject)
        return None


@dataclass
class FakeEmail:
    sent: list[dict] = field(default_factory=list)

    async def send(self, *, tenant_id, to, subject, body) -> str | None:
        self.sent.append({"tenant_id": tenant_id, "to": to, "subject": subject, "body": body})
        return f"fake-{len(self.sent)}"
