---
description: Create, review, and apply an Alembic migration safely — including backfills and zero-downtime sequencing.
argument-hint: <what is changing>
allowed-tools: Read, Write, Edit, Bash, Glob
---

Schema change: **$ARGUMENTS**

Use the `data-modeler` agent.

1. Edit the SQLAlchemy models first.
2. `alembic revision --autogenerate -m "<slug>"` then **read the generated file
   and fix it** — autogenerate misses enum changes, server defaults, index
   names, and anything about data.
3. Write a real `downgrade()`. If it's genuinely irreversible, say so in the
   docstring and explain the recovery path.
4. If the table has data in production, sequence it: add nullable → backfill in
   batches with a progress log → add `CHECK ... NOT VALID` → `VALIDATE
   CONSTRAINT` → set `NOT NULL`. Never a long `ACCESS EXCLUSIVE` lock.
5. Print the SQL: `alembic upgrade --sql head`.
6. Apply locally, then `alembic downgrade -1` and `upgrade head` again to prove
   reversibility. Run the test suite.

Report: migration id, DDL, lock impact, estimated runtime on a table of 1M rows,
and whether the deploy needs an expand/contract split.
