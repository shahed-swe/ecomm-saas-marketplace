import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';
import 'package:package_info_plus/package_info_plus.dart';

import 'tenant_config.dart';

ApiClient buildApiClient() {
  final config = TenantConfig.current;
  return ApiClient(
    baseUrl: config.apiBaseUrl,
    host: config.host,
    session: Session(baseUrl: config.apiBaseUrl, host: config.host),
  );
}

final apiProvider = Provider<ApiClient>((ref) => ref.watch(apiClientProvider));

final appConfigProvider = FutureProvider<AppConfig>((ref) async {
  final info = await PackageInfo.fromPlatform();
  final json = await ref.watch(apiProvider).get(
        Endpoints.appConfig,
        query: {
          'app': 'vendor',
          'version': info.version,
          'platform': defaultTargetPlatform == TargetPlatform.iOS ? 'ios' : 'android',
        },
      );
  return AppConfig.fromJson(json);
});

/// Vendor endpoints, kept here rather than in the shared core: the buyer app has no business
/// knowing they exist.
class VendorEndpoints {
  static const orders = '/api/v1/vendor/orders';
  static const shipments = '/api/v1/vendor/shipments';
  static const returns = '/api/v1/vendor/returns';
  static const balance = '/api/v1/vendor/balance';
  static const payouts = '/api/v1/vendor/payouts';
  static const scorecard = '/api/v1/vendor/scorecard';
  static String ready(String subOrderId) => '/api/v1/vendor/orders/$subOrderId/ready';
  static String ship(String subOrderId) => '/api/v1/vendor/orders/$subOrderId/ship';
  static String qc(String returnId) => '/api/v1/vendor/returns/$returnId/qc';
  static String decision(String returnId) => '/api/v1/vendor/returns/$returnId/decision';
}

final vendorOrdersProvider = FutureProvider.family<List<dynamic>, String?>((ref, status) {
  return ref.watch(apiProvider).getList(
        VendorEndpoints.orders,
        query: status == null ? null : {'status': status},
      );
});

final vendorReturnsProvider = FutureProvider<List<dynamic>>((ref) {
  return ref.watch(apiProvider).getList(VendorEndpoints.returns);
});

final vendorMoneyProvider = FutureProvider<Map<String, dynamic>>((ref) async {
  final api = ref.watch(apiProvider);
  final balance = await api.get(VendorEndpoints.balance);
  final payouts = await api.getList(VendorEndpoints.payouts);
  final scorecard = await api.get(VendorEndpoints.scorecard);
  return {'balance': balance, 'payouts': payouts, 'scorecard': scorecard};
});
