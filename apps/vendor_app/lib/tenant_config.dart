/// Build-time identity of one tenant's seller app — the same pattern as the buyer app.
class TenantConfig {
  const TenantConfig({required this.apiBaseUrl, required this.host, required this.bundleId});

  static const current = TenantConfig(
    apiBaseUrl: String.fromEnvironment('API_BASE_URL', defaultValue: 'https://api.example.com'),
    host: String.fromEnvironment('TENANT_HOST', defaultValue: 'demo.example.com'),
    bundleId: String.fromEnvironment('BUNDLE_ID', defaultValue: 'com.example.vendor'),
  );

  final String apiBaseUrl;
  final String host;
  final String bundleId;
}
