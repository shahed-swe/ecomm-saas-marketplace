"""Cache invalidation for storefront pages (architecture §5.3, §6). Every catalog write names the
ISR tags it dirties; a missed invalidation shows buyers a stale price."""

import hashlib
import hmac
import json
from dataclasses import dataclass, field

import httpx

from app.core.cache_keys import isr_tag
from app.core.logging import log


@dataclass
class RecordingRevalidator:
    """Tests and local dev: records tags."""

    calls: list[list[str]] = field(default_factory=list)

    async def revalidate(self, tenant_id: str, tags: list[str]) -> None:
        self.calls.append(tags)


class WebRevalidator:
    def __init__(self, url: str, secret: str):
        self.url = url
        self.secret = secret

    async def revalidate(self, tenant_id: str, tags: list[str]) -> None:
        body = json.dumps({"tags": tags}).encode()
        sig = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                await c.post(
                    self.url,
                    content=body,
                    headers={"content-type": "application/json", "x-signature": sig},
                )
        except httpx.HTTPError:
            # ISR TTL (60 s) is the backstop; log so a broken hook is visible.
            log.warning("revalidate_failed", tenant_id=tenant_id, tags=tags)


def product_tags(
    tenant_id: str, *, product_slug: str | None = None, category_ids=(), vendor_id=None
) -> list[str]:
    tags = [isr_tag(tenant_id, "products")]
    if product_slug:
        tags.append(isr_tag(tenant_id, "product", str(product_slug)))
    tags += [isr_tag(tenant_id, "category", str(c)) for c in category_ids if c]
    if vendor_id:
        tags.append(isr_tag(tenant_id, "store", str(vendor_id)))
    return tags
