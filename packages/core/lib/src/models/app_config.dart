/// The tenant's app configuration, fetched on launch (`/api/v1/app/config`).
class AppConfig {
  AppConfig({
    required this.storeName,
    required this.locale,
    required this.appName,
    required this.tokens,
    required this.gateways,
    required this.codEnabled,
    required this.features,
    required this.update,
    required this.maintenance,
    this.logoUrl,
    this.crashDsn,
  });

  factory AppConfig.fromJson(Map<String, dynamic> json) {
    final store = (json['store'] ?? {}) as Map<String, dynamic>;
    final branding = (json['branding'] ?? {}) as Map<String, dynamic>;
    final theme = (json['theme'] ?? {}) as Map<String, dynamic>;
    final payments = (json['payments'] ?? {}) as Map<String, dynamic>;
    return AppConfig(
      storeName: store['name'] as String? ?? '',
      locale: store['locale'] as String? ?? 'bn',
      appName: branding['app_name'] as String? ?? store['name'] as String? ?? '',
      logoUrl: branding['logo_url'] as String?,
      tokens: Map<String, dynamic>.from(theme['tokens'] as Map? ?? {}),
      gateways: List<String>.from(payments['gateways'] as List? ?? const []),
      codEnabled: payments['cod'] as bool? ?? false,
      features: Map<String, dynamic>.from(json['features'] as Map? ?? {}),
      update: UpdateGate.fromJson((json['update'] ?? {}) as Map<String, dynamic>),
      maintenance: Maintenance.fromJson((json['maintenance'] ?? {}) as Map<String, dynamic>),
      crashDsn: json['crash_dsn'] as String?,
    );
  }

  final String storeName;
  final String locale;
  final String appName;
  final String? logoUrl;
  final Map<String, dynamic> tokens;
  final List<String> gateways;
  final bool codEnabled;
  final Map<String, dynamic> features;
  final UpdateGate update;
  final Maintenance maintenance;
  final String? crashDsn;

  bool feature(String key) => features[key] == true;
}

class UpdateGate {
  UpdateGate({required this.state, this.storeUrl, this.message, this.latestVersion});

  factory UpdateGate.fromJson(Map<String, dynamic> json) => UpdateGate(
        state: json['state'] as String? ?? 'ok',
        storeUrl: json['store_url'] as String?,
        message: json['message'] as String?,
        latestVersion: json['latest_version'] as String?,
      );

  final String state; // ok | optional | force
  final String? storeUrl;
  final String? message;
  final String? latestVersion;

  bool get mustUpdate => state == 'force';
  bool get shouldUpdate => state == 'optional';
}

class Maintenance {
  Maintenance({required this.on, this.message});

  factory Maintenance.fromJson(Map<String, dynamic> json) =>
      Maintenance(on: json['on'] as bool? ?? false, message: json['message'] as String?);

  final bool on;
  final String? message;
}
