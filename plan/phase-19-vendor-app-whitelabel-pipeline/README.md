# Phase 19 — Vendor app & white-label build pipeline (3 weeks) ✅ COMPLETE (code written; not compiled here)

**Mission:** a seller's phone app for the parts of the job that happen away from a desk, and a pipeline that turns one codebase into any tenant's branded app in their **own** store account — without a signing key ever touching our database.

**Demo:** staff fill in the store profile (package names, track, and the *names* of the CI secrets), press build, and the queue hands that build to a runner; the runner resolves the secrets from its own store, generates the icon from the tenant's published brand colour, builds, signs, uploads to that tenant's Play track, and reports back — so the tenant sees "uploaded · internal track" rather than a CI log.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 19.1 | Migration: `app_store_profiles` (store identity + **secret references**, never secrets) and `app_builds` with a queue index | `migrations/versions/0019_app_builds.py` | ✅ |
| 19.2 | Build manifest per tenant app: bundle ids, host, locale, brand colours and logo from the published theme, store listing, secret names | `mobile/builds.manifest` | ✅ |
| 19.3 | Readiness check that refuses a build up front, naming what is missing, instead of failing in CI ten minutes later | `builds._ready`, `request_build` | ✅ |
| 19.4 | One build in flight per app+platform; build numbers increment per version | `builds.request_build` | ✅ |
| 19.5 | Runner queue with `FOR UPDATE SKIP LOCKED`, so two runners can never claim the same build | `builds.claim_next` | ✅ |
| 19.6 | Report-back endpoint (building / succeeded / failed / uploaded / rejected) with store status and error, visible to the tenant | `builds.report`, `mobile/platform_router.py` | ✅ |
| 19.7 | Staff APIs: store profile, request a build, build history | `mobile/router.py` | ✅ |
| 19.8 | GitHub Actions workflow: claim → analyze + test → brand → restore signing → build → report → upload to the tenant's store account | `infra/ci/mobile-release.yml` | ✅ |
| 19.9 | Branding script (icon and splash from the tenant's brand colour and logo, application id and display name) and Play upload script | `infra/scripts/brand_app.py`, `play_upload.py` | ✅ |
| 19.10 | Vendor app: today's orders with "packed" and "book courier", returns with approve/refuse and QC, money (available, reserve, holds, payouts, scorecard) | `apps/vendor_app/lib/**` | ✅ |

## Tests (397 api tests pass)
A build is refused until the app can actually be built, and the refusal names the missing package id or signing secret; once the profile is saved the manifest carries the package, the track and the **names** of secrets · a second build for the same app and platform is refused while one is in flight · two runners claiming at the same moment never receive the same build, and the manifest handed over carries the tenant's host and app name · CI's report is what the tenant reads: succeeded → uploaded with the store status, and a finished build frees the slot · a failed build keeps its reason and the next attempt gets build number 2 · the queue and the manifest are platform-only and 404 to a tenant's staff · the manifest carries the published brand colour, locale and store listing.

## Notes
- **No signing material in the database.** The profile stores secret *names*; the runner resolves them from GitHub secrets. A database dump is not a code-signing incident.
- Each tenant publishes under their own Play and App Store accounts: they own the listing, the reviews and the install base, and can leave with them. The pipeline only pushes builds.
- `analyze` and the widget tests run **before** anything is signed, so a broken build never reaches a store.
- As in Phase 18, the Flutter code is written here but compiled in CI: this container has no SDK and cannot download one.
- The workflow lives in `infra/ci/` rather than `.github/workflows/` because the deploy token in use has no `workflow` scope; copying it across is a one-line step when a token that does is available.
