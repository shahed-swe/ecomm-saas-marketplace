import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:marketplace_core/marketplace_core.dart';

/// Push registration.
///
/// The token is registered against the signed-in person, and re-registered whenever FCM rotates it
/// — a token that silently goes stale is a buyer who stops hearing about their own orders.
class PushRegistrar {
  PushRegistrar(this._api);

  final ApiClient _api;

  Future<void> start({String app = 'buyer', String locale = 'bn'}) async {
    final messaging = FirebaseMessaging.instance;
    final settings = await messaging.requestPermission(alert: true, badge: true, sound: true);
    if (settings.authorizationStatus == AuthorizationStatus.denied) return;
    final token = await messaging.getToken();
    if (token != null) await _register(token, app: app, locale: locale);
    messaging.onTokenRefresh.listen((fresh) => _register(fresh, app: app, locale: locale));
  }

  Future<void> _register(String token, {required String app, required String locale}) async {
    try {
      await _api.post(
        Endpoints.devices,
        body: {
          'token': token,
          'platform': defaultTargetPlatform == TargetPlatform.iOS ? 'ios' : 'android',
          'app': app,
          'locale': locale,
        },
      );
    } on ApiError {
      // Not being able to register for push must never stop someone from shopping.
    }
  }

  Future<void> stop(String token) async {
    try {
      await _api.delete('${Endpoints.devices}/$token');
    } on ApiError {
      // ignored on purpose, as above
    }
  }
}
