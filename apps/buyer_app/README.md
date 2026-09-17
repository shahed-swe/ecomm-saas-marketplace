# Buyer app (white-label)

One Flutter codebase; every tenant's app differs only in what CI passes in at build time and what
the API returns at runtime.

## What is build-time and what is not

| Build-time (`--dart-define`, store metadata) | Runtime (`GET /api/v1/app/config`) |
|---|---|
| bundle id / application id, signing, app name on the icon | theme colours and radii, logo, splash |
| `API_BASE_URL`, `TENANT_HOST` | payment methods, feature flags, locale |
| Firebase project files (`google-services.json`, `GoogleService-Info.plist`) | home layout (the page builder's sections) |
| store listing, screenshots | minimum supported version, maintenance message |

That split is the point: a tenant can rebrand, reprice and re-merchandise without waiting days for
a store review, and only a genuinely new binary needs a release.

## Running it

```bash
flutter pub get
flutter run \
  --dart-define=API_BASE_URL=https://api.example.com \
  --dart-define=TENANT_HOST=shop.example.com \
  --dart-define=BUNDLE_ID=com.rongin.buyer \
  --dart-define=APP_NAME="Rongin Bazar"
```

## Notes for whoever picks this up

* The refresh token lives in the platform keystore (`flutter_secure_storage`); the access token is
  kept in memory only and refreshed on a 401, once, before the request is retried.
* Money is passed around as a decimal **string** and formatted with Bangladeshi lakh/crore
  grouping. Do not parse it into a `double` on the way through.
* Checkout creates one idempotency key per attempt, so a dropped connection cannot produce two
  orders however many times the button is pressed.
* `SectionView` renders the tenant's published home sections and ignores section types it does not
  recognise, so an older app keeps working after the tenant publishes something new.
* This app was authored in an environment without the Flutter SDK (no network access to
  `storage.googleapis.com`), so it has not been compiled here. `flutter analyze` and the widget
  tests run in the mobile CI workflow in `infra/ci/`.
