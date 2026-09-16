.PHONY: up down test api-test web-test contract
up:        ; docker compose -f infra/docker-compose.yml up -d --build
down:      ; docker compose -f infra/docker-compose.yml down
api-test:  ; cd apps/api && uv run pytest -q
web-test:  ; pnpm -C apps/web typecheck && pnpm -C apps/web test
contract:  ; cd apps/api && uv run python scripts/export_openapi.py && pnpm -C packages/api-contract gen
test: api-test web-test
