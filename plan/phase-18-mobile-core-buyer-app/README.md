# Phase 18 — Mobile core & buyer app (4 weeks) ✅ COMPLETE (code written; not compiled here)

**Mission:** one Flutter codebase that becomes any tenant's branded buyer app, where everything that can change without a store review does change without one — colours, logo, home layout, payment methods, copy — and only a genuinely new binary needs a release.

**Demo:** the app launches, asks the API who it is, paints itself in the tenant's published theme, renders the home screen the tenant arranged in the page builder, and refuses to run at all when it is below the minimum supported version or the store is in maintenance. A buyer can search, add to cart, check out (with an idempotency key that survives a dropped connection), follow the parcel, and delete their account.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 18.1 | Migration: `app_configs`, `app_releases` (per app and platform), `account_deletion_requests` with a pending-unique index; RLS | `migrations/versions/0018_mobile.py` | ✅ |
| 18.2 | `GET /api/v1/app/config` — theme tokens, branding, payment methods, feature flags, crash DSN, maintenance and the **update gate answered** for the calling version | `mobile/router.py` | ✅ |
| 18.3 | Version comparison that treats versions as numbers (`1.10.0 > 1.9.0`), refusing a minimum newer than the latest | `router.update_state`, `_version_tuple` | ✅ |
| 18.4 | `GET /api/v1/app/home` delegating to the same server-driven page the web storefront renders | `mobile/router.app_home` | ✅ |
| 18.5 | Account deletion: 14-day grace, blocked while orders are in flight, cancellable, and an **anonymise** that keeps the sales record | `mobile/service.anonymise_user` | ✅ |
| 18.6 | Staff surfaces: app config, releases per platform, deletion queue with a manual complete | `mobile/router.py` | ✅ |
| 18.7 | Nightly job carrying out deletions that are due, once, with an audit entry | `workers/settings.account_deletions` | ✅ |
| 18.8 | `packages/core`: API client (tenant host, single retry on 401 refresh, idempotency keys), keystore-backed session, money as decimal strings with lakh/crore grouping, product/order/section models, cart controller, tenant theme → `ThemeData` | `packages/core/lib/**` | ✅ |
| 18.9 | Buyer app: launch gate, server-driven home, search, PDP with variants, cart, checkout (store credit, gateway handoff), orders with parcel tracking, account with the deletion flow | `apps/buyer_app/lib/**` | ✅ |
| 18.10 | Push registration that re-registers on FCM token rotation and never blocks shopping when it fails | `apps/buyer_app/lib/push.dart` | ✅ |

## Tests (392 api tests pass)
One call gives the app its whole face — name, theme tokens, gateways, feature flags, crash DSN — and the home it renders is section-for-section the web storefront's home · an app on the latest version is told `ok`, an older one `optional` with the store URL, and one below the minimum `force` with the tenant's message; a minimum newer than the latest is refused (422) · versions compare as numbers, so `1.10.0` is newer than `1.9.0` (the classic lockout bug) and a client that does not report its version is never blocked · maintenance mode replaces the whole app with the tenant's own wording · account deletion is blocked while an order is in flight, is idempotent, can be cancelled inside the grace period, and when carried out leaves the person anonymised (identifiers replaced, addresses and devices gone, sessions revoked) while the order, its total and the ledger survive with the customer scrubbed out · the scheduled job takes only what is due, never twice, and writes an audit entry · a device token registered by a second person on the same handset moves rather than duplicating.

## Notes
- **The Flutter code was written here but not compiled here.** This container has no Flutter SDK and the egress policy blocks `storage.googleapis.com`, so `flutter pub get`, `flutter analyze` and the widget tests have to run where the SDK is available — the mobile CI workflow added in Phase 19 does exactly that, and it is the gate before any store upload.
- What is build-time and what is runtime is written down in `apps/buyer_app/README.md`; the split is the whole reason a tenant can rebrand in an afternoon.
- `SectionView` ignores section types it does not recognise, so publishing a new section on the web never breaks an app that is a version behind.
- Deletion anonymises rather than erases, because a tenant must keep sales records for the NBR and a vendor must keep proof of what was sold. The buyer's own data — addresses, cart, devices, sessions, review text — goes; the money does not.
