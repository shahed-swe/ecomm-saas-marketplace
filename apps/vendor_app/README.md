# Vendor app (white-label)

The seller's three questions on a phone: what to pack, what came back, what am I owed. Built from
the same `packages/core` as the buyer app and branded the same way — build-time identity, runtime
theme.

```bash
flutter run \
  --dart-define=API_BASE_URL=https://api.example.com \
  --dart-define=TENANT_HOST=shop.example.com \
  --dart-define=BUNDLE_ID=com.rongin.seller
```

Deliberately small: a seller managing a catalogue sits at a computer, so the app covers the parts
that happen away from one — packing, booking a courier, judging a return, and checking the money.

Not compiled in the authoring environment (no Flutter SDK); `flutter analyze` runs in mobile CI.
