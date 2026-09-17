/// Build-time identity of one tenant's app.
///
/// Everything that can change without a release (colours, logo, home layout, payment methods) comes
/// from the API at runtime. Only what the stores tie to a binary lives here, and CI passes it in
/// with --dart-define, so one codebase produces every tenant's app.
class TenantConfig {
  const TenantConfig({
    required this.apiBaseUrl,
    required this.host,
    required this.bundleId,
    required this.fallbackName,
  });

  static const current = TenantConfig(
    apiBaseUrl: String.fromEnvironment('API_BASE_URL', defaultValue: 'https://api.example.com'),
    host: String.fromEnvironment('TENANT_HOST', defaultValue: 'demo.example.com'),
    bundleId: String.fromEnvironment('BUNDLE_ID', defaultValue: 'com.example.buyer'),
    fallbackName: String.fromEnvironment('APP_NAME', defaultValue: 'Marketplace'),
  );

  final String apiBaseUrl;
  final String host;
  final String bundleId;
  final String fallbackName;
}
