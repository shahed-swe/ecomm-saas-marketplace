---
name: flutter-marketplace-apps
description: Procedural knowledge for the Flutter mobile monorepo — Melos workspace configuration, Riverpod state management architecture, OpenAPI Dio client generation from the backend schema, secure token storage and silent refresh, durable SQLite offline operation queue with conflict resolution, multi-app flavor configurations (dev/staging/prod), push notification deep-link routing, and cross-tenant isolation in the vendor app. Load this skill when implementing Phase 1 mobile foundation, Phase 9 rider app, Phase 11 buyer app, Phase 12 vendor app, or any Flutter mobile work.
---

# Flutter Marketplace Apps — Procedural Skill

## 1. Melos workspace

### 1.1 Structure
```
mobile/
  melos.yaml
  packages/
    core/                   # shared across all 3 apps
      lib/
        api/                # generated Dio client + interceptors
        auth/               # secure-storage tokens, silent refresh, scopes
        design/             # theme tokens, colors, typography, shared widgets
        offline/            # durable op queue + sync engine
        push/               # FCM/APNs registration + deep-link routing
        money.dart          # Decimal formatting, currency display
        format.dart         # date/time, number formatting
        i18n/               # en.arb, bn.arb
      test/
      pubspec.yaml
  apps/
    buyer/                  # storefront, split cart, checkout, tracking
      lib/
        features/           # catalog/, search/, pdp/, cart/, checkout/,
                            # orders/, tracking/, account/
        app.dart
      android/ ios/
      pubspec.yaml
    vendor/                 # orders, stock, chat, earnings
      lib/
        features/           # orders/, inventory/, chat/, earnings/,
                            # settings/, staff/
        app.dart
      android/ ios/
      pubspec.yaml
    rider/                  # deliveries, navigation, PoD, COD cash
      lib/
        features/           # assigned/, delivery/, pod/, cod/, cash/
        app.dart
      android/ ios/
      pubspec.yaml
  integration_test/         # on-device flows (cross-tenant 404, COD cycle)
```

### 1.2 melos.yaml
```yaml
name: marketplace_mobile
packages:
  - packages/**
  - apps/**

scripts:
  gen:api:
    description: Regenerate OpenAPI Dio client
    run: |
      cd packages/core && dart run build_runner build --delete-conflicting-outputs
    select-package:
      scope: core

  test:
    description: Run tests across all packages
    run: flutter test
    exec:
      concurrency: 1

  build:buyer:dev:
    run: flutter build apk --flavor dev --target lib/main_dev.dart
    select-package:
      scope: buyer

  build:vendor:dev:
    run: flutter build apk --flavor dev --target lib/main_dev.dart
    select-package:
      scope: vendor

  build:rider:dev:
    run: flutter build apk --flavor dev --target lib/main_dev.dart
    select-package:
      scope: rider
```

---

## 2. OpenAPI Dio client generation

### 2.1 Generator setup
In `packages/core/pubspec.yaml`:
```yaml
dependencies:
  dio: ^5.4.0
  retrofit: ^4.1.0
  json_annotation: ^4.8.0

dev_dependencies:
  build_runner: ^2.4.0
  retrofit_generator: ^8.0.0
  json_serializable: ^6.7.0
  openapi_generator:
    path: ../../tools/openapi_generator  # or pub package
```

### 2.2 Generation from OpenAPI schema
```bash
# From project root:
curl http://localhost:8000/openapi.json -o mobile/packages/core/openapi.json
cd mobile/packages/core
dart run build_runner build --delete-conflicting-outputs
```

### 2.3 Interceptors
```dart
// packages/core/lib/api/interceptors.dart

class AuthInterceptor extends Interceptor {
  final TokenStorage _tokenStorage;

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    final token = _tokenStorage.accessToken;
    if (token != null) {
      options.headers['Authorization'] = 'Bearer $token';
    }
    handler.next(options);
  }

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) async {
    if (err.response?.statusCode == 401) {
      final refreshed = await _tokenStorage.silentRefresh();
      if (refreshed) {
        // Retry the original request with new token
        final retryResponse = await _retry(err.requestOptions);
        handler.resolve(retryResponse);
        return;
      }
    }
    handler.next(err);
  }
}

class IdempotencyInterceptor extends Interceptor {
  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    // Money-moving endpoints get an Idempotency-Key
    if (_isMoneyMoving(options.path, options.method)) {
      options.headers['Idempotency-Key'] ??= Uuid().v7();
    }
    handler.next(options);
  }

  bool _isMoneyMoving(String path, String method) {
    if (method != 'POST') return false;
    return path.contains('/checkout') ||
           path.contains('/cod/confirm') ||
           path.contains('/payout');
  }
}

class VendorScopeInterceptor extends Interceptor {
  // Only active in vendor app — adds vendor context header
  final String? vendorId;

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    if (vendorId != null) {
      options.headers['X-Vendor-Id'] = vendorId;
    }
    handler.next(options);
  }
}
```

---

## 3. Secure token storage

```dart
// packages/core/lib/auth/token_storage.dart

class TokenStorage {
  final FlutterSecureStorage _secure;
  String? _accessToken;  // in-memory only — never persisted

  // Access token: short-lived (15min), in memory
  String? get accessToken => _accessToken;

  // Refresh token: long-lived, in secure storage (Keychain/Keystore)
  Future<void> saveRefreshToken(String token) async {
    await _secure.write(key: 'refresh_token', value: token);
  }

  Future<bool> silentRefresh() async {
    final refreshToken = await _secure.read(key: 'refresh_token');
    if (refreshToken == null) return false;

    try {
      final response = await _dio.post('/auth/refresh', data: {
        'refresh_token': refreshToken,
      });
      _accessToken = response.data['access_token'];
      await saveRefreshToken(response.data['refresh_token']);
      return true;
    } catch (e) {
      await logout();  // family revoked — force re-login
      return false;
    }
  }

  // Rider app: token carries only 'rider' scope
  // Vendor app: token carries vendor staff scope
  TokenScope get scope => _decodeScope(_accessToken);
}
```

---

## 4. Riverpod state management

### 4.1 Architecture
```dart
// Feature-based provider organization
// packages/core/lib/providers/auth_provider.dart
final authProvider = StateNotifierProvider<AuthNotifier, AuthState>((ref) {
  return AuthNotifier(ref.read(tokenStorageProvider));
});

// apps/buyer/lib/features/cart/providers/cart_provider.dart
final groupedCartProvider = StateNotifierProvider<GroupedCartNotifier, GroupedCart>((ref) {
  return GroupedCartNotifier(ref.read(apiClientProvider));
});
```

### 4.2 Rules
- Providers are defined in the feature that owns them.
- Cross-feature providers live in `packages/core`.
- No provider computes price, tax, or commission — display what the API returns.
- Vendor app providers always include `vendor_id` in API calls.

---

## 5. Offline operation queue

### 5.1 Durable op queue (SQLite-backed)
```dart
// packages/core/lib/offline/op_queue.dart

class OfflineOpQueue {
  final Database _db;  // sqflite

  Future<void> enqueue(OfflineOp op) async {
    await _db.insert('offline_ops', {
      'id': op.id,  // UUID — durable, survives restart
      'type': op.type.name,
      'payload': jsonEncode(op.payload),
      'idempotency_key': op.idempotencyKey,
      'created_at': DateTime.now().toIso8601String(),
      'status': 'pending',
      'retry_count': 0,
    });
  }

  Future<void> sync() async {
    final pending = await _db.query('offline_ops',
        where: 'status = ?', whereArgs: ['pending'],
        orderBy: 'created_at ASC');

    for (final op in pending) {
      try {
        await _execute(op);
        await _db.update('offline_ops',
            {'status': 'completed'},
            where: 'id = ?', whereArgs: [op['id']]);
      } catch (e) {
        final retries = (op['retry_count'] as int) + 1;
        if (retries > 5) {
          await _db.update('offline_ops',
              {'status': 'failed', 'retry_count': retries},
              where: 'id = ?', whereArgs: [op['id']]);
        } else {
          await _db.update('offline_ops',
              {'retry_count': retries},
              where: 'id = ?', whereArgs: [op['id']]);
        }
      }
    }
  }
}
```

### 5.2 What queues offline
| App | Operations | Why |
|---|---|---|
| Buyer | Cart add/remove/update | Dead zone shouldn't lose a cart |
| Rider | PoD capture (OTP/photo) | Never lose a delivery proof |
| Rider | COD cash collection | Never lose a cash record |
| Rider | Status transitions | `picked_up → in_transit → arrived` |

### 5.3 Conflict resolution
- **Last-write-wins** for cart quantity updates.
- **Idempotency-key** prevents duplicate money operations.
- **Server-authoritative** for stock — if the server rejects (out of stock), the
  op is marked `conflict` and the UI shows the error.

---

## 6. Flavor configurations

### 6.1 Per-app flavors
```dart
// apps/buyer/lib/config/flavors.dart
enum Flavor { dev, staging, prod }

class FlavorConfig {
  static late Flavor current;

  static String get apiBaseUrl => switch (current) {
    Flavor.dev => 'http://10.0.2.2:8000',      // Android emulator
    Flavor.staging => 'https://staging-api.example.com',
    Flavor.prod => 'https://api.example.com',
  };

  static String get sentryDsn => switch (current) { ... };
}
```

### 6.2 Entry points
```
apps/buyer/lib/main_dev.dart     → Flavor.dev
apps/buyer/lib/main_staging.dart → Flavor.staging
apps/buyer/lib/main_prod.dart    → Flavor.prod
```

---

## 7. Push notification deep-link routing

```dart
// packages/core/lib/push/deep_link_router.dart

void handleDeepLink(Map<String, dynamic> data) {
  final type = data['type'];
  final id = data['id'];

  switch (type) {
    // Buyer app
    case 'order_status':
      navigateTo('/orders/$id');
    case 'tracking_update':
      navigateTo('/tracking/$id');

    // Vendor app
    case 'new_order':
      navigateTo('/vendor/orders/$id');
    case 'payout_completed':
      navigateTo('/vendor/earnings');

    // Rider app
    case 'delivery_assigned':
      navigateTo('/rider/deliveries/$id');
    case 'cash_remittance_due':
      navigateTo('/rider/cash');
  }
}
```

---

## 8. Cross-tenant isolation (vendor app)

```dart
// integration_test/vendor_isolation_test.dart

testWidgets('vendor A cannot access vendor B data', (tester) async {
  // Login as vendor A
  await loginAsVendor(vendorA);

  // Attempt to fetch vendor B's product by ID
  final response = await api.getProduct(vendorBProductId);
  expect(response.statusCode, 404);  // NOT 403

  // Attempt to fetch vendor B's order
  final orderResponse = await api.getSubOrder(vendorBSubOrderId);
  expect(orderResponse.statusCode, 404);

  // Verify no cached cross-tenant data
  final cachedProducts = await productProvider.getAll();
  expect(cachedProducts.every((p) => p.vendorId == vendorA.id), true);
});
```
