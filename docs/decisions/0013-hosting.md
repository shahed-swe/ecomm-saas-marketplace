# ADR 0013 — Hosting: local Bangladesh data centre
Status: Accepted · 2026-09-16

- Self-run: PostgreSQL 16 (pgBackRest WAL archive + base backups, streaming replica), Redis 7, MinIO, HashiCorp Vault, Caddy (on-demand TLS), Prometheus/Grafana/Loki, Sentry (self-hosted or SaaS if allowed).
- CDN in front for static + ISR HTML; origin data stays in BD.
- Off-site backup copy at a second BD location. RPO ≤ 5 min, RTO ≤ 2 h. Monthly restore drill from Phase 1.
- Environments: local (docker compose), staging (same topology, anonymised seed), production.
- Deploy: Docker images from CI, compose/Ansible to nodes; blue/green for api and web.
