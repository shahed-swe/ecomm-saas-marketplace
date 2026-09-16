---
description: Reason about, revise, or report status on the marketplace build plan in plan/ — architecture, tenancy, money, delivery, payments, mobile, and the phase sequence.
argument-hint: [status | revise <phase> | architecture <question>]
allowed-tools: Read, Write, Edit, Glob, Grep
---

Operate on the build plan: **$ARGUMENTS**

The plan lives in `plan/` — `00-architecture.md` (tenancy, order tree, ledger,
delivery, payments, budgets) and `phase-00..16-*/` (the executable sequence, 17
phase folders). `.claude/docs/ROADMAP.md` is the narrative. Use the
`principal-architect` agent for anything that changes the system shape or
resequences phases.

Interpret the argument:

- **`status`** (or empty) — scan the repo against each phase's Exit criteria and
  report a table: phase, done/in-progress/blocked, current open gate, next action.
  Cover all 17 phases (0–16). Flag loudly if any work has proceeded past a red
  tenancy or ledger gate.
- **`revise <phase>`** — a scope or design change landed. Update that phase folder
  and any downstream phase it affects, keeping the section skeleton. If it touches
  tenancy, the order tree, the ledger, delivery/COD, or payment rails, update
  `00-architecture.md` too and say so explicitly — those are the things that
  cannot be retrofitted.
- **`architecture <question>`** — answer from `00-architecture.md`; if it needs a
  real decision (commission, payout, fees, KYC, delivery model, gateway choice),
  it belongs in Phase 0's decisions doc — record it there, don't leave it in chat.

Rules:
- The plan is the source of truth; never let code and plan silently drift.
- Guard the tenancy, order-tree, ledger, and payment-verification invariants
  above all — a plan change that weakens isolation, breaks append-only accounting,
  or bypasses server-side verification is rejected, not merged.
- Do not expand into the deferred list without flagging a scope change.
- Keep edits small; preserve the terse, senior voice of the existing files.
