# Phase 1 — Foundation & infra (2 weeks) ✅ COMPLETE

**Mission:** the skeleton every phase bolts onto, with backups, staging and CI from day one.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 1.1 | API app factory, config, structured logs, request-id, problem+json errors | `apps/api/app/main.py`, `app/core/*` | ✅ |
| 1.2 | `/healthz`, `/readyz`, Prometheus `/metrics` (no tenant label) | `app/modules/health`, `app/core/metrics.py` | ✅ |
| 1.3 | Alembic baseline: extensions, `uuid_generate_v7()`, `app_current_tenant/vendor()`, append-only guard | `migrations/versions/0001_baseline.py` | ✅ |
| 1.4 | DB roles: migrator / app (NOBYPASSRLS) / platform | `infra/postgres/init/01-roles.sql` | ✅ |
| 1.5 | ARQ worker with `@tenant_job` (enqueue without tenant fails) | `app/workers/settings.py` | ✅ |
| 1.6 | OpenAPI export → `packages/api-contract` → TS types | `scripts/export_openapi.py` | ✅ |
| 1.7 | Next.js shell, route groups, CSS-variable tokens, injection-safe `tokensToCss` | `apps/web/*` | ✅ |
| 1.8 | Compose topology | `infra/docker-compose.yml` | ✅ |
| 1.9 | pgBackRest WAL + off-site repo, restore drill | `infra/pgbackrest`, `infra/scripts/restore-drill.sh` | ✅ |
| 1.10 | Caddy wildcard + on-demand TLS | `infra/caddy/Caddyfile` | ✅ (ask endpoint: Phase 2) |
| 1.11 | CI workflow | `infra/ci/github-ci.yml` | ✅ (move to `.github/workflows/` once token has `workflow` scope) |
| 1.12 | Deploy script | `infra/scripts/deploy.sh` | ✅ |

## Tests
- api: 9 pass · web: typecheck, theme injection test, production build

## Smoke
Migrations as `migrator` → API as `app` → `/readyz` ok → web serves storefront shell. (Docker Hub is blocked in the build environment; compose smoke runs on staging.)
