# Phase 6 — Vendor onboarding & management (3 weeks) ✅ COMPLETE

**Mission:** a stranger becomes an approved, payable seller inside one tenant: signup (open or invite-only), KYC straight to a private bucket, a real approval state machine with duplicate detection, and a payout destination guarded by re-authentication, a 72-hour hold and out-of-band SMS.

**Demo:** tenant opens invite-only signup → admin creates a zero-commission launch invite → seller signs up with it → uploads trade licence, NID, bank proof via signed PUT → submits (auto under review) → admin sees duplicate signals and views documents through 5-minute signed URLs → approves documents, then the vendor → owner re-auths by OTP and adds a bank account (encrypted, last-4 only) → changes to bKash → 72 h hold + SMS to owner and shop contact.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 6.1 | Migration: vendor profile columns, storefront logo/banner/shipping policy, tenant signup mode + required docs + default commission, `vendor_documents`, `vendor_events` (append-only), `vendor_invite_codes`, `commission_rules`, `vendor_payout_methods` (one active, ciphertext + keyed hash + last4), `payout_holds`; RLS on all | `migrations/versions/0006_vendor_onboarding.py` | ✅ |
| 6.2 | PII crypto: AES-GCM with context binding (Vault transit in prod), tenant-keyed HMAC hashes | `app/core/crypto.py` | ✅ |
| 6.3 | Private storage interface: signed PUT/GET, head; local + S3 implementations; bytes never transit the API in prod | `app/core/storage/private.py` | ✅ |
| 6.4 | Vendor state machine with performer rules (vendor/system/staff), events + audit, timestamps | `app/modules/vendors/lifecycle.py` | ✅ |
| 6.5 | Public signup: store mode + signup mode, invite codes (use-once/limits, tier, commission rule), plan quota, existing-account password check, agreement | `onboarding.py` | ✅ |
| 6.6 | KYC: upload URL (type/size allow-list, rate limit), register (key prefix must be caller's, object must exist), onboarding status, submit | same | ✅ |
| 6.7 | Admin review: profile, documents with signed view URLs, duplicate signals (document number, phone), events; document approve/reject (reason); transition (reason for negative moves; approval needs approved required docs; house vendor immutable) | same | ✅ |
| 6.8 | Operator tools: invite codes, per-vendor commission overrides with expiry, payout hold release (`payouts.approve`) | same | ✅ |
| 6.9 | Payout destination: OTP re-auth → single-use 5-min token → encrypted bank/bKash details, replace + 72 h hold on change, SMS to owner and shop contact, audit; owner only | same | ✅ |
| 6.10 | Vendor boundary: suspended vendors read-only (423); `require_vendor_role(..., approved=True)` for later catalog/order routes | `app/core/deps.py` | ✅ |

## Tests (102 api tests pass)
Signup closed/invite-only/open, invite single use (case-insensitive), launch tier + 0% rule, single-mode store refuses, agreement required · full journey to approval incl. premature submit/approve 409, bad content type 422, signed view URL serves the file with `private, no-store`, reject needs reason, suspension read-only then reinstated · storage key from another vendor → 404 (same and other tenant), duplicate NID + shared phone flagged · payout: no re-auth 401, wrong OTP 400, first set no hold, token single use, change → hold + SMS, ciphertext never contains the number and decrypts with context, manager forbidden · vendor_events append-only · isolation harness: review, transition, commission, document review, hold release (ISO-T + replay).

## Notes
- Real SMS provider and email notices arrive with the Phase 16 notification service; the SMS gateway interface is already used.
- KYC file virus scanning hook belongs in the Phase 7 media worker.
