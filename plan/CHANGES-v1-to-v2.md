# Changes Report — plan (v1) → plan-v2

What was added, removed, and fixed, and why. v1 stays untouched in `plan/` for reference.

## 1. New: SaaS platform layer
| Added | Why |
|---|---|
| Two-level tenancy (`tenant_id` + `vendor_id`, RLS on both, 2×2 isolation harness ISO-T/ISO-V) | Many clients on one platform; a cross-tenant leak is worse than cross-vendor |
| Host-based tenant resolution; JWT `tid` bound to host | Stops token replay across stores |
| Users per tenant; separate `platform_users` | White-label brands expect separate accounts |
| Store mode single/multi via *house vendor* (no code branch) | Decision "both"; avoids `if multi_vendor` leaks |
| Subdomain + custom domain, Caddy on-demand TLS with `ask` guard, DNS verification | Decision; prevents cert abuse |
| `(platform)` super-admin console, tenant lifecycle (trial → suspended → purged), audited impersonation | Operating a SaaS |
| Plans, entitlements, subscription + GMV fee + setup fee billing, dunning | Decision on monetisation |
| Per-tenant rate limits and job concurrency caps | Noisy-neighbour protection |

## 2. New: Theme & page builder
| Added | Why |
|---|---|
| Theme presets, design tokens as CSS variables, logo/favicon/fonts, dark mode | Client brands their store without code |
| Immutable `theme_versions` snapshots; draft → preview → publish → rollback by pointer flip | Safe edits, instant rollback |
| One section registry (JSON Schema) → Pydantic + TS forms + Dart models | Add a section once, works in API, builder, and app |
| ~20-section V1 library; home, header/footer/menus, custom pages, PDP/category block layouts | Decision on editable pages |
| Vendor limited store builder (accent color within palette, allowed sections) | Decision "owner full, vendor limited" |
| Custom CSS sandbox (parser, selector scoping, banned constructs, ignored on checkout) | Decision; stops XSS/phishing via CSS |
| WCAG AA contrast guard on publish; curated Latin + Bangla fonts | Client choices can't make the site unreadable |
| Mobile `/app/bootstrap` with tokens + server-driven home sections | Decision "color/logo + homepage" on app |

## 3. Changed: Payments
| Change | Why |
|---|---|
| **Removed Stripe Connect and Nagad** | Stripe unavailable for BD; Nagad not chosen for V1 |
| Money goes to **tenant's own** bKash/SSLCommerz accounts; credentials in Vault | Decision; platform avoids holding client GMV |
| Webhook path picks tenant, credential proves it | Prevents spoofed cross-tenant callbacks |
| Credential health check; broken method auto-hidden | Fewer failed checkouts |
| COD partial advance + blocklist + refusal-rate scoring | BD fake-order problem |
| Separate platform billing book (tenant pays us) | Two different money flows |

## 4. Changed: Delivery
| Change | Why |
|---|---|
| **Removed own rider fleet, rider app, dispatch engine, live map** | Decision "courier only" (~3 weeks saved) |
| Added `CourierAdapter` for Pathao, Steadfast, RedX; status normalisation; webhook + polling | National coverage |
| Courier selection rules by area/weight/COD | Cost and reliability |
| COD settlement statement matching → ledger | Couriers hold COD cash now |

## 5. New: Commerce features missing in v1
- Phone OTP login, guest checkout (OTP), BD geography address book (division → district → upazila → area)
- **Full returns/RMA:** request with photos → approve → reverse pickup → QC → refund / store credit, credit notes
- **Store credit wallet**
- **Promotions:** vendor coupons, flash sales/campaigns, free-shipping rules, defined ledger treatment and proration
- **VAT** config + Mushak-style invoices + credit notes; **TDS** on payouts
- **Admin-managed taxonomy:** categories + attributes (v1 had per-vendor categories), brands
- Bulk CSV product import, product Q&A, wishlist, recently viewed
- Support tickets, push campaigns, abandoned cart, GA4 + Meta Pixel/CAPI per tenant, cookie consent
- en/bn on web, Banglish search, per-host sitemaps + JSON-LD
- Tenant staff RBAC (owner/finance/support/moderator/content) with permission strings; audit log viewer

## 6. Changed: Payouts
- Stripe transfers → **payout batch → bank file (BEFTN/NPSB) or bKash list → finance approve (maker-checker) → mark paid with reference**
- Payout destination change keeps re-auth + OTP + 72 h hold + dual notification

## 7. Fixed: contradictions found in v1
| v1 problem | v2 fix |
|---|---|
| Ledger account names differ across docs (`buyer_payment` vs `platform_clearing`…) | One chart of accounts, architecture §7.2 |
| Three different `entry_type` enums; signed vs direction amounts | One enum; `amount > 0` + direction |
| `sub_orders` status enum mismatch (DATA-MODEL vs phase 8) | Will be single-sourced in `02-data-model.md` |
| Categories per vendor vs category commission/facets | Tenant-level taxonomy |
| Brand indexed in search but no brand table | `brands` table in Phase 7 |
| Reserve rule double-withholding (percent + window) | One reserve rule per tenant |
| COD capture at checkout (P6) vs at remittance (ADR) | COD posts at courier-confirmed delivery, settles at statement match |
| Vendor app (P12) needs chat/push built in P13/P14 | Trust/messaging (15) and notifications (16) now precede apps (18–19) |
| Buyer app needs push before notifications phase | Same reorder |
| Two device-token tables, two messaging schemas | Single owner: Phase 15 (chat), Phase 16 (devices) |
| ADR numbers/filenames inconsistent | Renumbered in Phase 0 v2, one manifest |
| Overlapping week ranges | Sequential weeks, explicit buffers |
| Backups/staging only at launch | PITR, restore drill, staging in Phase 1 |
| No force update / account deletion / crash reporting in apps | Phase 18 exit criteria |
| `settings.json` hooks use `$CLAUDE_FILE_PATHS` (not set); no `mobile/**` edit permission | To fix when updating `.claude` for v2 |

## 8. New: Infrastructure (local BD data centre)
Self-run Postgres (pgBackRest PITR, replica), Redis, MinIO (replicated off-site), Vault; CDN in front; OpenTelemetry + Prometheus/Grafana/Loki + Sentry; RPO ≤ 5 min, RTO ≤ 2 h; monthly restore drill.

## 9. Removed or deferred
Stripe Connect, Nagad, own rider fleet + rider app + dispatch, KYC-via-Stripe; custom JS in themes, theme marketplace, multi-currency, multi-warehouse, buyer subscriptions, auctions, B2B, vendor ads, Kubernetes.

## 10. Risks to tell clients / accept
1. **Timeline:** ~13–14 months solo with everything in V1. Checkpoints A/B/C let you demo and sell earlier.
2. **App Store 4.2.6:** white-label apps from one template can be rejected; mitigated by tenant-owned developer accounts and real branding, not guaranteed.
3. **Tenant-owned gateways:** each client must obtain its own bKash/SSLCommerz merchant accounts and courier accounts; onboarding can stall there.
4. **Local DC:** we own ops (backups, patching, failover); budget time monthly.
5. **Custom CSS:** a client can still make their own site ugly; the builder protects checkout and contrast, not taste.
6. **Tax rates:** VAT/TDS values are configuration confirmed by each client's accountant, not legal advice from the plan.

## 11. Next files to write (after approval)
`02-data-model.md` (v2 single source of columns/enums) · `03-scenarios.md` (Given/When/Then with ISO-T, ISO-V, LDG) · `01-file-manifest.md` · 21 `phase-NN-*/` folders · updated `.claude/` bundle (CLAUDE.md, new agents: saas-platform-engineer, theme-builder-engineer, courier-integration-engineer, tax-invoicing-engineer; fixed settings.json).
