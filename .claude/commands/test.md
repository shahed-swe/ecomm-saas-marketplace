---
description: Write or repair tests for a module, run the suite, and explain every failure.
argument-hint: [module or path]
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Target: **${ARGUMENTS:-the whole suite}**

Use the `qa-test-engineer` agent.

1. Run what exists first and read the output before changing anything.
2. For each failure: reproduce, state the root cause in one sentence, then fix
   the *cause*. Do not weaken an assertion to get green.
3. Fill gaps: every endpoint needs happy path, 401, 403, 422. Every money or
   stock path needs a concurrency test. Every expiry needs a frozen-clock test.
4. Report coverage for the target module, but call out which uncovered lines
   actually matter instead of chasing a percentage.

Finish with: the command to reproduce, the pass/fail counts, and a short list of
known gaps.
