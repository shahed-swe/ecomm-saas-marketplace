---
description: Review the current diff like a senior engineer — correctness, security, performance, and conventions.
argument-hint: [base branch, default main]
allowed-tools: Read, Bash, Glob, Grep
---

Review the diff against **${ARGUMENTS:-main}**.

Read `git diff ${ARGUMENTS:-main}...HEAD` in full, plus the surrounding code for
anything you flag. Then report in this order:

1. **Blocking** — correctness bugs, security holes, data loss, money errors,
   missing authorisation, unsafe migration.
2. **Should fix** — N+1s, missing index, missing test, unhandled error state,
   convention violations against CLAUDE.md.
3. **Consider** — naming, structure, simplification.
4. **Good** — one or two things genuinely done well. Be specific, not flattering.

Rules: quote `file:line`. Propose the fix, don't just name the problem. If you
disagree with an approach, say so directly and give your reasoning. If the diff
is clean, say it's clean — don't manufacture findings.

Run `security-reviewer` automatically if the diff touches auth, payments,
uploads, or `/admin`.
