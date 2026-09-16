---
description: Pre-launch gate — run every check, fix blockers, and produce the go/no-go report.
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Run the full release gate. Do not declare ready until each line is verified by
an actual command, not by assumption.

**Correctness** — full test suite green; migrations apply on a clean database
and downgrade cleanly; seed script works.

**Security** — `security-reviewer` full pass; no secrets in git history; admin
routes guarded; rate limits live; CSP and security headers set; dependency audit
(`pip-audit`, `pnpm audit`) with no unpatched highs.

**Performance** — LCP < 2.0s mobile, first-load JS < 180 kB per route, no N+1 on
the catalogue or order endpoints, Redis cache hit path verified.

**Payments** — webhook signature verification tested, duplicate and out-of-order
events handled, refund path exercised in sandbox, COD cap enforced.

**Ops** — health and readiness endpoints, structured logs with request ids,
Sentry receiving events, backups scheduled *and restored once*, rollback
procedure written down.

**Content & legal** — 404/500 pages, robots.txt, sitemap, OG images, returns and
privacy pages present.

Output a go/no-go table with one line per item: status, evidence (the command
and its result), and owner for anything red.
