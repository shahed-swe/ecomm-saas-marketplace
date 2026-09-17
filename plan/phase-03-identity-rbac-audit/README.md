# Phase 3 — Identity, RBAC & audit (2 weeks) ✅ COMPLETE

**Mission:** every human boundary gets real authentication: buyers (email+password, phone OTP), tenant staff with permission bundles, vendor staff roles, platform super-admins with TOTP — and every mutation lands in an append-only audit log.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 3.1 | Migration: `users` (per tenant, unique email/phone per tenant, BD phone check), `refresh_tokens`, `otp_challenges`, `staff_roles`, `staff_members`, `vendor_users` (one owner), `audit_log` (append-only trigger, no UPDATE grant), `platform_users` (no app grant) — RLS on all tenant tables | `migrations/versions/0003_identity.py` | ✅ |
| 3.2 | Password hashing (argon2id, dummy verify for unknown users), BD phone normalisation | `app/core/passwords.py`, `app/core/phone.py` | ✅ |
| 3.3 | Register / login per surface (`buyer`, `staff`, `vendor`) with identical failure responses | `app/modules/identity/service.py`, `router.py` | ✅ |
| 3.4 | Refresh rotation in families; reuse ⇒ family revoked **and committed**; httpOnly cookie for web, body for apps (`X-Client: app`); logout | same | ✅ |
| 3.5 | Phone OTP: HMAC-hashed codes, 5-min TTL, 5 attempts persisted on failure, 60 s resend throttle, per-phone and per-IP rate limits, single use | same, `app/core/ratelimit.py`, `sms.py` | ✅ |
| 3.6 | Permission catalogue + system roles (owner, finance, support, moderator, content); permissions read from DB each request | `app/core/permissions.py`, `app/core/deps.py` | ✅ |
| 3.7 | Vendor boundary re-checks membership + vendor state per request; vendor role permissions | `app/core/deps.py` | ✅ |
| 3.8 | Staff management (invite, change role, disable; owner immutable; no self-edit); vendor staff management (manager limits) | `app/modules/identity/staff_router.py` | ✅ |
| 3.9 | Audit writer (secrets scrubbed) + admin viewer with filters and keyset paging; audit on login, staff, vendor, storefront, domain changes | `app/core/audit.py`, routers | ✅ |
| 3.10 | Platform login with password + TOTP | `identity/router.py` | ✅ |
| 3.11 | Tenant creation seeds roles and the owner (staff owner + house-vendor owner) | `app/modules/platform/service.py` | ✅ |

## Tests (64 api tests pass)
- Refresh rotation, replay ⇒ whole family dead; app vs web token delivery; logout revokes
- Generic 401 for wrong password / wrong surface / unknown user
- Accounts per tenant; token from tenant 1 refused on tenant 2
- OTP: invalid phone 422, throttled resend sends nothing, 5 wrong codes lock the challenge (persisted), single use, one user created, rate limit 429
- Staff: seeded roles, owner not invitable, support has `vendors.read` but not `staff.manage`/`audit.read`, audit entry recorded, **disabling staff revokes access before token expiry**
- Vendor: staff role limits, login for foreign vendor refused, disabling member revokes immediately
- Audit log: runtime roles lack UPDATE, owner blocked by trigger
- Platform: TOTP required; app role cannot read `platform_users`
- Isolation harness extended to staff and vendor-staff routes (ISO-T, ISO-V, replay)

## Deferred to their owning phases
- Guest checkout token (Phase 10), re-auth OTP for payout changes (Phase 6), email verification + password reset mails (Phase 16 notification service; endpoints stubbed by OTP flow), web login screens (built with the admin/vendor consoles).
