# Phase 9 — Discovery, search, SEO & Bangla (3 weeks) ✅ COMPLETE

**Mission:** buyers find products fast in English, Bangla or Banglish — with facets that never leak another tenant's data — and search engines index each tenant's own domain.

**Budget:** search < 200 ms p95 @ 20k products / 100 vendors (architecture §14). **Measured: p95 ≈ 120–145 ms** in the automated perf test (includes the HTTP stack), ~25 ms for typical queries on the smoke database.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 9.1 | Migration: GIN on `search_vector`, trigram + jsonb indexes, `search_terms` (distinct words, trigram), `search_synonyms`, `wishlist_items`, `search_candidates()` | `migrations/versions/0009_search.py` | ✅ |
| 9.2 | **RLS vs GIN:** Postgres will not use an index for non-leakproof operators (`@@`, `@>`) under RLS, so every search scanned the tenant's rows (1.7 s p95 in the first perf run). Fixed with a `SECURITY DEFINER` plpgsql function scoped to `app_current_tenant()` that builds only the predicates in use (custom plan per call); result cards are re-loaded under RLS by primary key | same, `app/modules/catalog/search.py` | ✅ |
| 9.3 | Query expansion: built-in Banglish↔Bangla shopping dictionary + tenant synonyms (Redis-cached, invalidated on change); tsquery built only from regex-clean tokens (no operator injection) | `search.py` | ✅ |
| 9.4 | Typo handling: when a query finds nothing, correct each word against the tenant's own words (trigram on a small table), re-run, return `did_you_mean` | `search.py`, `index_terms` on product writes | ✅ |
| 9.5 | Search API: category (incl. children), brands, stores, price range, in stock, attribute filters, 4 sorts, pagination; facets (categories, brands, stores, filterable attributes, price range) from the same tenant-visible set; 2 queries | `GET /api/v1/catalog/search` | ✅ |
| 9.6 | Type-ahead suggestions (products + categories, one query) | `GET /api/v1/catalog/suggest` | ✅ |
| 9.7 | Admin synonyms CRUD | `POST/GET/DELETE /api/v1/admin/catalog/synonyms` | ✅ |
| 9.8 | Wishlist (idempotent, visible products only) and recently viewed (Redis list, capped 30, tenant-prefixed) | `app/modules/catalog/buyer.py` | ✅ |
| 9.9 | Sitemap feed (visible products, categories, stores, published custom pages) | `app/modules/catalog/seo.py` | ✅ |
| 9.10 | Web: search page with facet filters and sort; store page (vendor accent + sections); per-host `sitemap.xml` and `robots.txt`; PDP metadata, canonical to primary host, Open Graph, Product + Breadcrumb JSON-LD (script-safe) | `apps/web/src/app/...` | ✅ |
| 9.11 | Web i18n: en/bn message dictionaries, cookie language toggle (tenant default otherwise), Bangla digits | `messages/`, `lib/locale.ts`, `api/locale` | ✅ |

## Tests (184 api tests pass)
tsquery built from clean tokens (SQL/tsquery injection strings neutralised) · results + facets + synonyms + Bangla expansion + typo correction + filters, tenant B never appears · suggest · wishlist/recently viewed incl. cross-tenant 404 and vendor 403 · sitemap lists only visible tenant URLs · **perf test: 20k products, 100 vendors, 10 query shapes × 4 → p95 < 200 ms** · isolation harness extended (synonym delete).
Web smoke on a 20k-product tenant: Bangla results count "৩০০৩টি ফলাফল", sitemap XML per host, robots.
