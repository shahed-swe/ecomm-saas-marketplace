---
name: nextjs-commerce-ui
description: Build storefront and admin UI in Next.js App Router with Tailwind and Radix — product listing and detail pages, variant selectors, cart, checkout, search and filters, order tracking, and admin tables. Use this skill for any customer-facing or admin screen in this project, including rendering strategy, caching and revalidation, SEO metadata, and Core Web Vitals decisions, even when the request is phrased as "just add a page".
---

# Next.js Commerce UI

## Rendering decision table — pick one, say which, and why

| Surface | Strategy | Invalidation |
|---|---|---|
| Home / rails | Static + ISR 300s | `revalidateTag('home')` on admin publish |
| Category listing | Static + ISR 600s, dynamic when filters are in the URL | `revalidateTag('category:<id>')` |
| Product detail | Static + ISR 900s | `revalidateTag('product:<id>')` on write |
| Search | Dynamic Server Component, `no-store` | — |
| Cart / checkout | Client, Zustand + server quote | — |
| Account / orders | Dynamic, per-request auth | — |
| Admin | Dynamic, client tables | — |

The admin write path calls `POST /api/revalidate` with a secret; the route
handler calls `revalidateTag`. Without this, ISR silently serves stale prices —
which is the one stale value customers will notice.

## Price and stock: server-authoritative

The cart holds ids and quantities. Price, discount, tax, and availability come
from `POST /checkout/quote` on every render of the cart and again at order
creation. Never persist a price in localStorage and display it as truth.

## Component inventory (build once, reuse)

`ProductCard` · `PriceTag` (handles compare-at, currency, minor units) ·
`VariantSelector` (disables out-of-stock combinations, reflects selection in the
URL) · `QuantityStepper` · `AddToCartButton` (optimistic, rolls back with a
visible error) · `CartSheet` · `FilterSidebar` (URL-synced via
`useSearchParams`, so filters are shareable and back-button-correct) ·
`OrderStatusTimeline` · `DataTable` (admin, server-paginated).

## Variant selector logic

Build a matrix of option combinations from the variant list. A colour swatch is
disabled only if *no* in-stock variant exists for that colour given the other
selected options. Reset downstream options when an upstream one changes. Encode
the selection as `?variant=<id>` so the PDP is shareable and the correct image
and price render on the server.

## Cart state

```ts
// Zustand slice — ids and quantities only
type CartItem = { variantId: string; qty: number }
```
Persist to localStorage. On login, `POST /cart/merge` with the local items; the
server returns the authoritative cart and the client replaces its state. Handle
the "item went out of stock while you were away" case with an explicit inline
notice on the line, not a silent removal.

## Checkout

Single page, three collapsible sections (contact → shipping → payment), not a
multi-route wizard — fewer navigations, less state loss. Address form uses a
country-aware schema. Show the recomputed quote after every change with a
subtle pending state, never a full-page spinner. Disable the submit button while
the intent is in flight and guard against double submit with an idempotency key
generated once per checkout session.

## SEO

`generateMetadata` per route: title, description, canonical, OG image from the
media pipeline's `card` variant. JSON-LD: `Product` + `Offer` + `AggregateRating`
on the PDP, `BreadcrumbList` on listings, `Organization` in the root layout.
`sitemap.ts` generated from the product and category tables. Out-of-stock
products stay reachable with `availability: OutOfStock` rather than 404-ing.

## Performance guardrails
- One `priority` image per page (the PDP hero, the first card above the fold).
- Fixed aspect ratio boxes everywhere — CLS budget 0.05.
- `dynamic()` for the rich-text editor, charts, and anything admin-only.
- Route first-load JS under 180 kB; check `next build` output before you commit.

## Accessibility that actually matters here
Announce cart changes with `aria-live="polite"`. Price must be readable text,
not an image. Variant swatches are radio groups with real labels. The checkout
form reports server-side field errors next to the field and moves focus to the
first invalid input.

## Marketplace additions

- **Three route groups, three layouts**: `(shop)`, `(vendor)`, `(admin)`. Each has
  its own auth boundary and nav. Never a shared layout with role conditionals.
- **Cart groups by vendor** — see the `marketplace-checkout-split` skill for the
  quote shape and per-group shipping.
- **Vendor card on the PDP**: name, logo, rating, response time, ships-from,
  return policy, "visit store". In a marketplace, trust signals convert.
- **Store pages** `/store/[slug]` — banner, bio, policies, their catalogue with
  its own facets. Static + ISR, revalidated on vendor or product write.
- **Vendor dashboard** never renders another vendor's numbers, including in
  comparative charts. Show "you rank #7 of 340", not a ranked revenue table.
- **Search facets** include store and ships-from; keep the vendor filter sticky
  across pagination.
