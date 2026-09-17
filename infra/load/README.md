# Load profile

`k6-marketplace.js` runs three tenants at once and asserts a per-tenant threshold, because the
number that matters in a multi-tenant system is not "how many requests per second" but "does a
neighbour's flash sale show up in my p95".

What to watch while it runs:

* `browse_ms{tenant:1}` — the quiet neighbour. If it tracks tenant 0 upward, isolation is failing
  somewhere shared: a connection pool, Redis, or a query without a tenant predicate that the
  planner is scanning wholesale.
* Postgres `pg_stat_activity` — connection count per role; the app pool should be bounded and the
  platform pool nearly idle.
* `search_candidates` timings — the RLS-safe search path (ADR in Phase 9) is the query most likely
  to degrade first as a catalogue grows.

The checkout path is deliberately **not** in this script: hammering it would create real orders,
reservations and ledger entries. Checkout is load-tested against a disposable tenant with the
`perf` pytest marker (`uv run pytest -m perf`), which builds its own data and cleans up.
