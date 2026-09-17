# Restore and the quarterly drill

Backups nobody has restored are not backups. `infra/scripts/restore-drill.sh` restores the latest
pgBackRest backup into a scratch database and runs the assertions below; it is scheduled quarterly
and must be run by hand after any schema change big enough to worry about.

## Drill

```bash
infra/scripts/restore-drill.sh --stanza main --target latest
```

The drill passes only if, on the restored copy:

1. `alembic current` matches the migration head in the repository;
2. every tenant table still has RLS enabled and forced (the same query as the audit test);
3. `SELECT sum(CASE WHEN direction='debit' THEN amount ELSE -amount END) FROM ledger_entries
   GROUP BY tenant_id, group_id HAVING ...` returns no unbalanced group;
4. a known order number from the drill fixture reads back with the same grand total.

Record the wall-clock time. **That number is the RTO** we can honestly quote; anything else is a
guess.

## Real restore

1. Stop the API and workers (`docker compose stop api worker`) so nothing writes while you decide.
2. Restore to the point in time *before* the damage: `pgbackrest --stanza=main --type=time
   --target="YYYY-MM-DD HH:MM:SS+06" restore`.
3. Start Postgres, run the drill assertions, **then** start the API.
4. Re-run `reconcile_payments` and the courier sweep: anything that happened after the restore
   point is re-derived from the providers rather than invented.
5. Tell affected tenants what window was lost, in their own dashboard and by email. A tenant who
   discovers a missing order themselves never trusts the platform again.
