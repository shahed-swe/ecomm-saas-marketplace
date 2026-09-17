# Phase 7 — Catalog, media & taxonomy (3 weeks) ✅ COMPLETE

**Mission:** vendors build a real catalogue on a tenant-level taxonomy — categories with typed attributes, brands, products with variants and a stock ledger, photos through a proper pipeline, moderation, bulk CSV import and product Q&A — and buyers see it fast on the storefront.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 7.1 | Migration: `categories` (tenant taxonomy, depth ≤ 4, path array), `category_attributes`, `brands`, `products` (moderation, rollups, `search_vector`), `product_variants` (stock ≥ 0, reserved ≤ on hand, SKU unique per vendor, option signature unique per product), `inventory_movements` (append-only), `media_assets`, `product_media`, `product_questions`, `import_jobs`; tenant `moderation_mode`; RLS on all | `migrations/versions/0007_catalog.py` | ✅ |
| 7.2 | Admin taxonomy: categories (nesting, depth limit), attributes (typed, options, required, filterable, inherited by children), brands; public tree + attributes + brands | `app/modules/catalog/taxonomy.py` | ✅ |
| 7.3 | Vendor products: create with variants, attribute validation, HTML stripped, generic conflicts, plan product quota; update; publish requires photo + required attributes | `products.py` | ✅ |
| 7.4 | Moderation: `none` / `first_listing` / `all`; admin queue + approve/reject (reason) | same | ✅ |
| 7.5 | Stock: row lock + CHECK + append-only movement with balance; concurrency-safe | `adjust_stock` | ✅ |
| 7.6 | Image pipeline: size gate, magic bytes, bomb guard, verify, EXIF transpose, ICC→sRGB, metadata stripped, 160/400/800/1600 (no upscale), AVIF q55 + WebP q82 + JPEG 800, 16 px blur, checksum dedupe, immutable keys; worker job + inline dev queue | `media.py`, `workers/settings.py` | ✅ |
| 7.7 | Public catalog: keyset listing (category incl. children, brand, store) in one query; PDP in 3 queries with vendor card; only approved products of approved vendors | `products.py` | ✅ |
| 7.8 | Cache invalidation: every write emits tenant-prefixed tags; signed web revalidate endpoint; web tags aligned with API tags | `revalidate.py`, `apps/web/src/app/api/revalidate` | ✅ |
| 7.9 | CSV import: template, grouping by handle, upsert by SKU (price + stock via ledger), per-row errors (≤200 kept), quota, worker + inline | `imports.py` | ✅ |
| 7.10 | Q&A: buyers ask (rate-limited), vendor answers (publishes), moderators hide | `products.py` | ✅ |
| 7.11 | Storefront: category grid, PDP, `<picture>` AVIF/WebP/JPEG with blur in reserved boxes, ৳ formatting | `apps/web/src/app/(shop)/{c,p}`, `components/catalog` | ✅ |

## Tests (163 api tests pass)
Taxonomy rules and inheritance · validation (bad/unknown attributes, compare-at, duplicate options, HTML stripped, generic slug conflict, foreign-tenant category 404) · publish requires photo · first-listing moderation, rejection reason, auto-approval after first, listing pagination with parent category, suspended vendor disappears · **PDP ≤ 3 queries** (measured) · **3 concurrent −2 on stock 3 → one succeeds, ledger sums to on-hand** · price change emits product/products tags · pipeline: AVIF 400px < 30 KB, 4 widths, JPEG fallback, blur, GPS/EXIF stripped, small image not upscaled, dedupe, bad/truncated files · CSV: grouped variants, Bangla titles, row errors, update via re-import, missing columns · Q&A scoping and publishing · isolation harness extended to 16 catalog routes (vendor ISO-T/ISO-V/replay/own, admin ISO-T/replay).

## Notes
- Vendor dashboard screens for catalog management come with the vendor console UI; all APIs are ready and typed.
- Virus scanning of KYC/CSV uploads can hook into the worker before processing.
