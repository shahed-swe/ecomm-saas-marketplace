# ADR 0012 — Theme contract: presentation as versioned data
Status: Accepted · 2026-09-16

- No per-tenant code. Presentation = JSON validated against a section registry (JSON Schema in `packages/theme-schema`), generating Pydantic, TypeScript and Dart types.
- `theme_presets` (platform), `tenant_themes(tenant_id, preset_id, draft_version_id, published_version_id)`, immutable `theme_versions(tenant_id, number, document jsonb, created_by, created_at, note)`.
- Publish = insert version + pointer flip in one transaction; rollback = pointer flip; preview = signed token rendering draft, uncached.
- Tokens are CSS variables; components contain no raw colors. Dark palette required.
- Contrast guard (WCAG AA) blocks publish.
- Custom CSS: parsed, banned constructs rejected, selectors prefixed, 50 KB cap, ignored on checkout/payment/account-security pages.
- Vendor store theme: accent within tenant palette, banner, logo, `vendor_allowed` sections only.
- Mobile: `/app/bootstrap` delivers tokens, brand, home sections, remote config, min version.
