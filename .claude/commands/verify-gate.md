---
description: Verify a phase's gate-in prerequisites and exit criteria without executing the phase — a dry-run check that reports red/green status for every gate condition.
argument-hint: <phase number, 0–16> [gate-in | gate-out | both]
allowed-tools: Read, Grep, Glob, Bash
---

Verify the gate for **Phase $ARGUMENTS** without executing any work.

1. **Resolve the phase directory.** Zero-pad the argument to two digits. Glob for
   `plan/phase-{NN}-*/` and read that folder's `README.md`.
2. **Determine check type.** If the second argument is `gate-in`, check only §0
   (Gate-in). If `gate-out`, check only §8 (Exit criteria). Default is `both`.
3. **Gate-in verification (§0).** For each checkbox in the Gate-in section:
   - If it references a previous phase's exit, verify that phase's Exit criteria
     are met (recursively if needed).
   - If it references a specific artefact (table, file, test, migration), check
     the codebase for its existence and correctness.
   - If it references tenancy isolation, check that `test_isolation.py` passes.
   - If it references ledger balance, check that `test_ledger.py` passes.
   - If it references a specific `.env` variable or service, check `.env.example`.
4. **Exit criteria verification (§8).** For each exit criterion:
   - Check for the existence and pass/fail status of the referenced tests.
   - Check that migrations + RLS policies exist.
   - Check that performance budget targets are not regressed.
   - Verify the Demo script (§9) prerequisites are in place.
5. **Report.** A table with: condition, status (✅ green / ❌ red / ⚠️ unknown),
   evidence (file path, test name, or reason). Summarise with a verdict:
   `GATE OPEN` or `GATE BLOCKED (N conditions red)`.

Rules:
- This is a **read-only** check — never modify files or run destructive commands.
- A red tenancy or ledger gate is always a hard stop — flag it prominently.
- If a gate condition references something that doesn't exist yet (e.g. the phase
  hasn't been built), report it as red with "not yet implemented".
- Unknown conditions (can't determine from codebase) are reported as ⚠️, not ✅.
