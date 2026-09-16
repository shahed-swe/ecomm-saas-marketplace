---
description: Manage the Flutter mobile monorepo — regenerate OpenAPI Dio clients, run tests across all 3 apps, check offline-queue invariants, build flavors, and verify cross-tenant isolation on-device.
argument-hint: [gen | test | build <app> <flavor> | offline-audit | isolation]
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Operate on the Flutter mobile monorepo: **$ARGUMENTS**

Use the `mobile-engineer` agent. Skill: `flutter-marketplace-apps`.

Interpret the argument:

- **`gen`** — regenerate the OpenAPI Dio client in `packages/core/lib/api/` from
  the current backend OpenAPI schema. Verify the generated types compile and match
  the latest endpoint additions. Report any breaking changes.
- **`test`** — run `melos run test` across all packages and apps. Report per-app
  results. For the vendor app, verify the cross-tenant 404 assertion exists.
- **`build <app> <flavor>`** — build a specific app (`buyer`, `vendor`, `rider`)
  with a specific flavor (`dev`, `staging`, `prod`). Report build size and any
  warnings.
- **`offline-audit`** — scan the offline op-queue implementations in
  `packages/core/lib/offline/` and each app. Verify: buyer cart operations queue
  and sync; rider PoD and COD-collect actions have durable op IDs; conflict
  resolution is defined; no money-moving op can be lost in a dead zone.
- **`isolation`** — check that the vendor app's test suite includes on-device
  cross-tenant assertions (request another vendor's data → 404). Check that the
  rider app only accesses assigned deliveries.

Rules:
- The Dio client is generated, never hand-written. If the schema changed,
  regenerate — don't patch.
- Riverpod is the state management, decided once in `packages/core`.
- `Idempotency-Key` on every money-moving call (checkout, COD confirm, payout).
- Access token in memory, refresh token in secure storage (Keychain/Keystore).
- Deep links route push notifications into the correct screen and correct app.
