---
description: Scaffold the whole monorepo from an empty directory — Next.js app, FastAPI service, Flutter mobile monorepo, Postgres/Redis/MinIO compose, auth, and the media pipeline skeleton.
argument-hint: [project-name]
allowed-tools: Read, Write, Edit, Bash, Glob
---

Bootstrap the project **$1** end to end. Work in this order and stop at each
checkpoint to report status.

1. **Repo skeleton** — `apps/web`, `apps/api`, `mobile/`, `infra/`, `docs/decisions/`,
   root `README.md`, `.gitignore`, `.editorconfig`, `.env.example` (all vars, no
   secrets), `Makefile` with `make dev`, `make test`, `make migrate`,
   `pnpm-workspace.yaml`, `melos.yaml`.
2. **API** — FastAPI app factory, settings via `pydantic-settings`, async
   SQLAlchemy session (with RLS `app.vendor_id` hook), Alembic initialised,
   `AppError` hierarchy + global handler, `/health` and `/ready`, structured
   logging, CORS from env.
3. **Auth module** — register, login, refresh, logout, me. Argon2id, JWT access
   + rotating refresh cookie with family-reuse revocation, `require_role`
   dependency. Tests included.
4. **Web** — Next.js App Router + TypeScript + Tailwind + Radix, `(shop)`,
   `(vendor)`, and `(admin)` route groups, each with its own layout and auth
   boundary, base layout, theme tokens in `tailwind.config.ts`,
   OpenAPI client generation script (`make gen:api`).
5. **Mobile** — Melos monorepo under `mobile/`:
   - `packages/core/` — OpenAPI Dio client generation, secure token storage +
     refresh, design system (theme tokens, shared widgets), money/format,
     i18n (en, bn), offline op-queue primitive.
   - `apps/buyer/` — Flutter buyer app skeleton with flavors (dev/staging/prod),
     login screen → home placeholder.
   - `apps/vendor/` — Flutter vendor app skeleton with flavors, scoped to
     `current_vendor`.
   - `apps/rider/` — Flutter rider app skeleton with flavors, rider-scoped JWT.
6. **Infra** — `docker-compose.yml` with web, api, worker, postgres (16, with
   pgcrypto + citext + pg_trgm extensions, RLS roles), redis (7), minio
   (**two buckets**: `media-public` and `kyc-private`), caddy; health checks.
   Gateway sandbox mock stubs for bKash, Nagad, SSLCommerz (optional, for local
   dev without live credentials).
7. **Seed** — script creating: 1 platform admin, 3 approved vendors with logos
   and policies, 1 pending vendor awaiting approval, 1 suspended vendor, 30
   products spread across them, 5 orders that produce sub-orders across two
   vendors, and a rider profile — so every tenancy, split, delivery, and
   payment path is exercisable from `make seed`.
8. **Verify** — bring the stack up, run migrations, hit `/health`, run the test
   suite. Additionally: log in as vendor A and confirm vendor B's product id
   returns 404. Print the URLs for all services.

Use the `fastapi-architect`, `nextjs-engineer`, `mobile-engineer`, and
`devops-release` agents for their respective sections. Report exactly what a
developer must fill into `.env` before this runs.
