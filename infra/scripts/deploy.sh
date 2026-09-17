#!/usr/bin/env bash
# Deploy to staging/production nodes in the BD data centre (ADR 0013).
# Usage: ENV=staging TAG=<git-sha> REGISTRY=<host> SMOKE_HOST=<host> ./infra/scripts/deploy.sh
# Order: build+push images -> migrations (expand-only) -> rolling api/web/worker -> smoke.
set -euo pipefail
: "${ENV:?staging|production}" "${TAG:?image tag}" "${REGISTRY:?private registry host}"
HOSTS_FILE="infra/hosts/${ENV}.txt"
COMPOSE="docker compose -f infra/docker-compose.yml -f infra/compose.${ENV}.yml"

docker build -t "$REGISTRY/ecomm-api:$TAG" apps/api
docker build -t "$REGISTRY/ecomm-web:$TAG" -f apps/web/Dockerfile .
docker push "$REGISTRY/ecomm-api:$TAG"; docker push "$REGISTRY/ecomm-web:$TAG"

PRIMARY=$(head -n1 "$HOSTS_FILE")
ssh "$PRIMARY" "TAG=$TAG $COMPOSE run --rm migrate"

while read -r host; do
  echo "[deploy] $host"
  ssh "$host" "TAG=$TAG $COMPOSE up -d --no-deps --scale api=2 --scale web=2 api web worker"
  ssh "$host" "for i in \$(seq 1 30); do curl -fsS localhost/readyz && break; sleep 2; done"
  ssh "$host" "TAG=$TAG $COMPOSE up -d --no-deps --remove-orphans --scale api=1 --scale web=1 api web"
done < "$HOSTS_FILE"

curl -fsS "https://${SMOKE_HOST:?}/readyz" >/dev/null && echo "[deploy] OK $ENV@$TAG"
