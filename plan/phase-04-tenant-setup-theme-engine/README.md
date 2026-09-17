# Phase 4 — Tenant setup & theme engine (2 weeks) ✅ COMPLETE

**Mission:** a client brands their store without code — preset themes, colours, fonts, dark mode and logo — with draft → preview → publish → rollback, and a storefront that renders the tenant's tokens before first paint.

**Demo:** create tenant → storefront renders the default theme → apply "Bazaar" preset in draft (public site unchanged) → preview link shows it → publish (v2 live) → rollback to v1 in one call → upload a logo, re-encoded to WebP.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 4.1 | Migration: `tenant_themes` (draft + published pointer), `theme_versions` (append-only trigger, INSERT-only grant), RLS | `migrations/versions/0004_theme.py` | ✅ |
| 4.2 | Theme document contract (Pydantic source → JSON Schema): palette (strict RGB), dark palette, radius/fonts/button/density enums, brand assets restricted to own uploads, `extra=forbid` | `app/modules/theme/schema.py`, `packages/theme-schema/` | ✅ |
| 4.3 | Presets: Minimal, Bazaar, Fashion (all contrast-clean, tested) | `app/modules/theme/presets.py` | ✅ |
| 4.4 | WCAG contrast guard (light + dark) blocks publish with exact pairs/ratios | `app/modules/theme/contrast.py` | ✅ |
| 4.5 | Draft / preset / publish (new immutable version + pointer flip) / versions / restore (pointer flip) / preview token (tenant-bound, 15 min, no-store) | `app/modules/theme/service.py`, `router.py` | ✅ |
| 4.6 | Public theme endpoint with Redis cache (`t:{tid}:theme:published`), ETag/304, invalidated on publish/rollback | same | ✅ |
| 4.7 | Brand image upload: magic bytes, 2 MB cap, bomb guard, verify, EXIF transpose, re-encode WebP+PNG ≤512px in threadpool, tenant-prefixed immutable keys | `app/modules/theme/images.py`, `app/core/storage/` | ✅ |
| 4.8 | Tenant settings (name, locale, support contact) + go-live onboarding checklist | `app/modules/settings/router.py` | ✅ |
| 4.9 | New tenants get a published default theme | `app/modules/platform/service.py` | ✅ |
| 4.10 | Storefront layout injects validated CSS variables (light, dark, system-dark), font stacks, logo | `apps/web/src/lib/theme.ts`, `(shop)/layout.tsx` | ✅ |
| 4.11 | Isolation guard fixed: walks OpenAPI paths (FastAPI wraps included routers); theme restore added to harness | `app/tests/test_isolation.py` | ✅ |

## Tests (77 api + web theme test)
- Presets pass contrast; WCAG reference ratios
- New tenant: v1 published, ETag → 304
- Draft invisible publicly; preview token shows draft with `no-store`; publish → v2 live; restore v1; audit entries
- Preview token from tenant 1 ignored on tenant 2
- Low contrast → 422 naming the failing pair
- Injection attempts rejected: CSS in colours, radius, fonts, foreign logo URL, extra keys, another tenant's asset path
- Upload: re-encoded WebP within bounds; SVG, fake PNG polyglot and >2 MB rejected
- Permissions: content role can publish; support cannot read theme
- theme_versions immutable (owner hits trigger); cross-tenant restore 404
- Settings update normalises phone and renames store; onboarding reflects state
- Web: validated CSS output, alias keys, dark + system dark, font stacks, injection filtered
- **E2E smoke:** API + web standalone: tenant host renders store name and its CSS variables; unknown host 404

## Notes
- Pydantic is the single source of the theme contract; JSON Schema is the generated artifact (ADR 0012 intent kept, direction clarified).
- Full media pipeline (AVIF, sizes, blur placeholders, dedupe) is Phase 7; brand images reuse its validation rules now.
- Builder UI (sections, drag and drop, custom CSS) is Phase 8.
