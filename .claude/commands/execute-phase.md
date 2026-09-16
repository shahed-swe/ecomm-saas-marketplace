---
description: Execute one phase of the marketplace build plan end to end — check its gate, work the breakdown in order, run the exit criteria.
argument-hint: <phase number, 0–16>
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Execute **Phase $ARGUMENTS** of the multi-vendor build plan.

1. **Resolve the phase directory.** Zero-pad the argument to two digits (e.g.
   `0` → `00`, `9` → `09`, `16` → `16`). Glob for `plan/phase-{NN}-*/` and
   read that folder's `README.md`. If no match, report the error and stop.
2. **Load context.** Read `plan/00-architecture.md` (once, if not already loaded)
   — especially §3 tenancy, §4 order tree, §5 money, §12 delivery, §13
   payments. Read `CLAUDE.md §10` (Definition of Done), §12 (budgets), §3
   (tenancy rule), and §7 (mobile rules where applicable).
3. **Check the Gate-in.** Verify every Gate-in checkbox is actually true in the
   codebase. If any is false, STOP and report which gate is red. In this repo the
   gates are existential — **never start Phase 2+ if the Phase-1 cross-tenant
   isolation tests are not passing at both the app and DB layer.**
4. **Read the phase's `files.md`** — the exact slice of the file manifest this
   phase builds, in dependency order, each mapped to its acceptance test and the
   scenario it satisfies.
5. **Work the "Work breakdown" top to bottom.** Delegate each task to the agent it
   names (`vendor-platform-engineer`, `payments-payouts-engineer`,
   `dispatch-delivery-engineer`, `marketplace-search-engineer`,
   `trust-safety-engineer`, `mobile-engineer`, …) or run the command it names
   (`/vendor-module`, `/payout-run`, `/dispatch`, `/mobile`, …). Produce the
   Files, write the Migration (with its RLS policy), satisfy the Acceptance
   bullets.
6. **Hold the budget & invariants.** After each task: the phase's §5 budget still
   holds; `vendor_id` filtering is in the repository not the caller; where money
   moved, ledger groups balance (debits = credits); no aggregate leaks cross-vendor
   data; payment callbacks are idempotent; COD cash reconciles.
7. **Run the Test plan (§6)** — including the mandatory cross-tenant isolation test
   (A→B by id → 404) and, where money moves, the ledger-balance test — and the
   **Exit criteria (§8)**. Run the **Demo script (§9)**.
8. **Report.** Tasks done, migrations + RLS policies added, budget numbers,
   isolation/ledger test results, any red Exit box (with why), and whether the
   gate to the next phase is open.

A cross-tenant leak or an unbalanced ledger is a P0 — treat a failure here as a
stop, not a note. Never mark the phase complete with a red Exit box or a failing
isolation/ledger test.
