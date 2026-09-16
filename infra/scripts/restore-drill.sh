#!/usr/bin/env bash
# Monthly restore drill (ADR 0013). Restores the latest backup (or a PITR target)
# into a throwaway instance, runs integrity checks, records duration against RTO <= 2h.
set -euo pipefail
TARGET_TIME="${1:-}"
WORK=$(mktemp -d)
START=$(date +%s)
echo "[drill] restoring into $WORK"
if [[ -n "$TARGET_TIME" ]]; then
  pgbackrest --stanza=ecomm --pg1-path="$WORK" --type=time --target="$TARGET_TIME" restore
else
  pgbackrest --stanza=ecomm --pg1-path="$WORK" restore
fi
pg_ctl -D "$WORK" -o "-p 55432 -c archive_mode=off" -w start
psql -p 55432 -d ecomm -v ON_ERROR_STOP=1 <<'SQL'
SELECT count(*) AS tables FROM information_schema.tables WHERE table_schema='public';
SELECT version_num FROM alembic_version;
-- Ledger integrity (from Phase 14): every group balances.
DO $$ BEGIN
  IF to_regclass('public.ledger_entries') IS NOT NULL THEN
    IF EXISTS (SELECT 1 FROM ledger_entries GROUP BY group_id
               HAVING sum(CASE direction WHEN 'debit' THEN amount ELSE -amount END) <> 0) THEN
      RAISE EXCEPTION 'unbalanced ledger group after restore';
    END IF;
  END IF;
END $$;
SQL
pg_ctl -D "$WORK" -w stop
rm -rf "$WORK"
echo "[drill] OK in $(( $(date +%s) - START ))s (RTO budget 7200s)"
