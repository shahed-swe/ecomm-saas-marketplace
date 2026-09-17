# marketplace_core

What the buyer and vendor apps share: the API client (tenant host, single-flight refresh,
idempotency keys), the session (keystore-backed refresh token), the tenant theme mapping, the
server-driven section model and the cart controller.

Kept deliberately thin: anything that is one app's business — screens, routing, store metadata —
belongs in that app, not here.
