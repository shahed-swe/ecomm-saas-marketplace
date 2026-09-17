import 'package:dio/dio.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Where the app's identity lives.
///
/// The refresh token goes to the platform keystore (Keychain / EncryptedSharedPreferences), never
/// to plain preferences; the access token stays in memory only, so a stolen backup of the device
/// yields nothing that works on its own.
class Session {
  Session({
    required this.baseUrl,
    required this.host,
    FlutterSecureStorage? storage,
    Dio? dio,
    this.locale = 'bn',
  })  : _storage = storage ?? const FlutterSecureStorage(),
        _dio = dio ?? Dio(BaseOptions(baseUrl: baseUrl));

  static const _refreshKey = 'refresh_token';

  final String baseUrl;
  final String host;
  final FlutterSecureStorage _storage;
  final Dio _dio;
  String locale;

  String? _accessToken;
  DateTime? _expiresAt;

  bool get isSignedIn => _accessToken != null;

  Future<String?> accessToken() async {
    if (_accessToken != null && (_expiresAt?.isAfter(DateTime.now()) ?? false)) {
      return _accessToken;
    }
    if (await refresh()) return _accessToken;
    return null;
  }

  Future<void> adopt(Map<String, dynamic> tokens) async {
    _accessToken = tokens['access_token'] as String?;
    final seconds = tokens['expires_in'] as int? ?? 900;
    _expiresAt = DateTime.now().add(Duration(seconds: seconds - 30));
    final refreshToken = tokens['refresh_token'] as String?;
    if (refreshToken != null) {
      await _storage.write(key: _refreshKey, value: refreshToken);
    }
  }

  Future<bool> refresh() async {
    final refreshToken = await _storage.read(key: _refreshKey);
    if (refreshToken == null) return false;
    try {
      final response = await _dio.post<Map<String, dynamic>>(
        '/api/v1/auth/refresh',
        options: Options(headers: {'host': host, 'x-forwarded-host': host}),
        data: {'refresh_token': refreshToken},
      );
      if (response.data == null) return false;
      await adopt(response.data!);
      return true;
    } on DioException {
      // A refused refresh means the family was revoked: forget everything and sign in again.
      await signOut();
      return false;
    }
  }

  Future<void> signOut() async {
    _accessToken = null;
    _expiresAt = null;
    await _storage.delete(key: _refreshKey);
  }
}
