---
description: Profile the app end to end and produce a prioritised optimisation plan with measured numbers.
argument-hint: [route or endpoint to focus on]
allowed-tools: Read, Edit, Bash, Glob, Grep
---

Audit scope: **${ARGUMENTS:-full stack}**

Use the `perf-optimizer` agent.

**Backend** — enable SQL echo, exercise the main flows, count queries per
endpoint, `EXPLAIN (ANALYZE, BUFFERS)` the slowest three, check pool settings,
list what should be cached and with what invalidation.

**Frontend** — `next build`, capture the route JS table, run Lighthouse mobile,
record LCP/CLS/INP, check image formats actually served, look for render-blocking
resources and client components that could be server components.

**Media** — sample ten product images: original bytes vs AVIF/WebP served, and
whether `sizes` matches the rendered box.

Deliver a table: issue → measured cost → proposed fix → expected win → effort.
Sort by win/effort. Implement only the top item, measure it, and report the real
delta before touching the next one.
