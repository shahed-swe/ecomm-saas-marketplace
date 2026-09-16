# ADR 0014 — Identity: per-tenant users, OTP, guest
Status: Accepted · 2026-09-16

- `users(tenant_id, email citext null, phone e164 null, password_hash null, status)`; unique `(tenant_id, email)`, `(tenant_id, phone)`; at least one of email/phone.
- `platform_users` separate table and auth for super-admins (TOTP required).
- Login: email+password (argon2id); phone OTP (6 digits, 5 min TTL, hashed, max 5 attempts, resend throttle, per tenant+phone+IP rate limits); guest checkout via OTP-verified phone and signed `guest_token`.
- Tokens: JWT access 15 min (`sub, tid, roles, vid?`), refresh rotating family in httpOnly cookie (web) / secure storage (apps); reuse detection revokes family.
- Tenant staff RBAC: permissions strings; seeded roles owner, finance, support, moderator, content; owner immutable.
- Vendor staff roles: owner, manager, staff.
- SMS provider behind `SmsGateway` interface; per-tenant sender ID optional.
