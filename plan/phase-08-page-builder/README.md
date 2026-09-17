# Phase 8 — Page builder (4 weeks) ✅ COMPLETE

**Mission:** tenants arrange their storefront without code — drag-and-drop sections on the home page, category and product pages, custom pages, header/footer/menus, product-page block layout — with autosaved drafts, preview, publish and rollback; vendors get a limited store-page builder; advanced tenants get sandboxed custom CSS.

**Demo (automated, `apps/web/e2e/builder.e2e.mjs`):** staff signs in → builder loads the draft → section reordered by drag → FAQ added from the library → autosave → publish → storefront immediately renders the new order (revalidation hook).

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 8.1 | Theme document v2: layouts (header menu, footer columns/social), templates (home, category_top, product_bottom, product_layout blocks, collection_layout), custom pages (reserved slugs, SEO, published flag), custom_css; v1 documents still valid | `app/modules/theme/schema.py`, `sections.py` | ✅ |
| 8.2 | Section registry (20 types) with per-type settings models, allowed pages, vendor_allowed, max per page; ids unique; ≤ 25 per page; safe hrefs (paths or https), own-asset images only, video/social host allow-lists, bilingual text | `sections.py` | ✅ |
| 8.3 | Registry endpoint exposing JSON Schemas (drives builder forms) | `GET /api/v1/theme/sections` | ✅ |
| 8.4 | Storefront resolver: published or previewed sections + data in a fixed budget (one query for all product sources via window/lateral, one each for categories, brands, vendors); custom pages; layouts | `storefront.py` | ✅ |
| 8.5 | Custom CSS sandbox (tinycss2): style rules + @media only, url() only to own media, blocked constructs, no fixed overlays, selectors scoped to `[data-tenant-css]`, 50 KB, plan-gated; served as its own stylesheet, skipped on checkout/payment/security pages | `css.py`, `storefront.py`, `(shop)/layout.tsx` | ✅ |
| 8.6 | Preset switch changes look only (keeps brand, sections, pages, CSS); document size cap 64 KB; every media path in the document must be the tenant's | `service.py` | ✅ |
| 8.7 | Publish/rollback trigger web revalidation (`t:{tid}:theme`) | `router.py` | ✅ |
| 8.8 | Vendor store builder: accent (contrast-checked against tenant palette), banner, ≤ 10 vendor-allowed sections, draft/publish; public store page with sections + products | `vendor_store.py`, migration `0008` | ✅ |
| 8.9 | Web: section renderer (20 components, escaped text, sandboxed video iframe), themed header/footer, home + custom pages | `components/sections`, `(shop)` | ✅ |
| 8.10 | Web: staff login, builder (dnd-kit sortable with keyboard support, add from library, JSON-Schema form generator, hide/delete, autosave, desktop/mobile iframe preview, publish, version restore) | `(admin)/admin/{login,builder}`, `components/builder` | ✅ |

## Tests
- API: 176 tests pass. New: starter home resolves only this tenant's products · rich home (9 sections, 4 data kinds) ≤ 6 queries and manual order kept · 6 invalid-section cases · placement/limits/duplicate ids/reserved slugs/social hosts · custom page publish + draft 404 · preset keeps content, rollback restores sections · CSS sanitizer (scoping + 11 attack cases) · custom CSS plan gate, preview stylesheet · vendor builder: disallowed section, low-contrast accent, publish, store page isolation.
- Web: typecheck + build; **browser E2E passed** (found and fixed: `crypto.randomUUID` unavailable on non-HTTPS preview hosts; array defaults now honour `minItems`).
