import 'dart:async';

import 'package:dio/dio.dart';

import '../state/session.dart';

/// One HTTP client for the whole app.
///
/// Three things it takes care of so no screen has to:
///  * the tenant's own host goes on every request, because the API decides the tenant from it;
///  * a 401 refreshes the session once and retries, so a long-lived app never bounces the user
///    to the sign-in screen for an expired access token;
///  * every mutation carries an Idempotency-Key, because mobile networks retry by themselves.
class ApiClient {
  ApiClient({
    required this.baseUrl,
    required this.host,
    required this.session,
    Dio? dio,
  }) : _dio = dio ?? Dio() {
    _dio.options
      ..baseUrl = baseUrl
      ..connectTimeout = const Duration(seconds: 15)
      ..receiveTimeout = const Duration(seconds: 30)
      ..headers['host'] = host
      ..headers['x-forwarded-host'] = host;
    _dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) async {
          final token = await session.accessToken();
          if (token != null) {
            options.headers['authorization'] = 'Bearer $token';
          }
          options.headers['accept-language'] = session.locale;
          handler.next(options);
        },
        onError: (error, handler) async {
          final response = error.response;
          final isAuth = response?.statusCode == 401;
          final alreadyRetried = error.requestOptions.extra['retried'] == true;
          if (isAuth && !alreadyRetried && await session.refresh()) {
            final options = error.requestOptions..extra['retried'] = true;
            try {
              handler.resolve(await _dio.fetch(options));
              return;
            } on DioException catch (retryError) {
              handler.next(retryError);
              return;
            }
          }
          handler.next(error);
        },
      ),
    );
  }

  final String baseUrl;
  final String host;
  final Session session;
  final Dio _dio;

  Future<Map<String, dynamic>> get(String path, {Map<String, dynamic>? query}) async {
    final response = await _dio.get<Map<String, dynamic>>(path, queryParameters: query);
    return response.data ?? <String, dynamic>{};
  }

  Future<List<dynamic>> getList(String path, {Map<String, dynamic>? query}) async {
    final response = await _dio.get<List<dynamic>>(path, queryParameters: query);
    return response.data ?? <dynamic>[];
  }

  Future<Map<String, dynamic>> post(
    String path, {
    Object? body,
    String? idempotencyKey,
  }) async {
    final response = await _dio.post<Map<String, dynamic>>(
      path,
      data: body,
      options: Options(
        headers: idempotencyKey == null ? null : {'idempotency-key': idempotencyKey},
      ),
    );
    return response.data ?? <String, dynamic>{};
  }

  Future<Map<String, dynamic>> patch(String path, {Object? body}) async {
    final response = await _dio.patch<Map<String, dynamic>>(path, data: body);
    return response.data ?? <String, dynamic>{};
  }

  Future<void> delete(String path) => _dio.delete<void>(path);
}

/// An error the user should see, separated from the noise of transport failures.
class ApiError implements Exception {
  ApiError(this.status, this.detail, {this.code});

  factory ApiError.from(DioException error) {
    final data = error.response?.data;
    if (data is Map && data['detail'] is String) {
      return ApiError(
        error.response?.statusCode ?? 0,
        data['detail'] as String,
        code: data['title'] as String?,
      );
    }
    return ApiError(error.response?.statusCode ?? 0, 'Something went wrong. Please try again.');
  }

  final int status;
  final String detail;
  final String? code;

  @override
  String toString() => detail;
}
