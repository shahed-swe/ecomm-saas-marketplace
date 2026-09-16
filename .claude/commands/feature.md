---
description: Implement a full vertical slice — migration, API, client types, UI, tests — for a described feature.
argument-hint: <feature description>
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Build this feature end to end: **$ARGUMENTS**

Follow the sequence; do not skip ahead.

1. **Clarify in one pass.** List the entities, the endpoints, the permissions,
   and the edge cases you inferred. State assumptions inline — only ask me if
   something is genuinely undecidable from the codebase.
2. **Schema** — `data-modeler`: tables, constraints, indexes, migration.
3. **API** — `fastapi-architect`: module slice, schemas, service rules, routes.
4. **Contract** — regenerate the typed client (`pnpm gen:api`).
5. **UI** — `nextjs-engineer`: screens with loading/empty/error states, forms
   with Zod validation, optimistic updates where it helps.
6. **Tests** — `qa-test-engineer`: service unit tests, endpoint integration
   tests, one e2e if the feature is on a critical path.
7. **Review** — run `security-reviewer` if the feature touches auth, money,
   uploads, or admin.

Finish with the Definition of Done checklist from CLAUDE.md §9, ticked or
explicitly marked N/A with a reason.

## Marketplace additions

Before step 2, answer explicitly: **is this data vendor-scoped, platform-wide, or
both?** If vendor-scoped, use `/vendor-module` instead of this command. If both,
build two routers (`/vendor/...` and `/admin/...`) with separate dependencies —
never one router with a role branch inside it.

If the feature touches money, the `payments-payouts-engineer` agent must review
the ledger entries before you call it done.
