import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';
import 'package:package_info_plus/package_info_plus.dart';

import 'tenant_config.dart';

final sessionProvider = Provider<Session>((ref) => ref.watch(apiProvider).session);

/// One client for the whole app; the core's providers receive the same instance.
ApiClient buildApiClient() {
  final config = TenantConfig.current;
  return ApiClient(
    baseUrl: config.apiBaseUrl,
    host: config.host,
    session: Session(baseUrl: config.apiBaseUrl, host: config.host),
  );
}

final apiProvider = Provider<ApiClient>((ref) => ref.watch(apiClientProvider));

/// Launch call: theme, branding, payment methods, feature flags and the update gate in one round trip.
final appConfigProvider = FutureProvider<AppConfig>((ref) async {
  final api = ref.watch(apiProvider);
  final info = await PackageInfo.fromPlatform();
  final json = await api.get(
    Endpoints.appConfig,
    query: {'version': info.version, 'platform': _platform()},
  );
  return AppConfig.fromJson(json);
});

final homeSectionsProvider = FutureProvider<List<Section>>((ref) async {
  final api = ref.watch(apiProvider);
  final page = await api.get(Endpoints.appHome);
  return ((page['sections'] ?? []) as List)
      .map((s) => Section.fromJson(Map<String, dynamic>.from(s as Map)))
      .toList();
});

/// The store the build came from decides which release rules apply to it.
String _platform() => defaultTargetPlatform == TargetPlatform.iOS ? 'ios' : 'android';
