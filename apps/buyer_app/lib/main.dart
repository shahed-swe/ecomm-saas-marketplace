import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import 'providers.dart';
import 'screens/home_screen.dart';
import 'screens/gate_screen.dart';

void main() {
  runApp(
    ProviderScope(
      overrides: [
        // The core's cart controller needs the app's client: one client, one session, one host.
        apiClientProvider.overrideValue(buildApiClient()),
      ],
      child: const BuyerApp(),
    ),
  );
}

class BuyerApp extends ConsumerWidget {
  const BuyerApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final config = ref.watch(appConfigProvider);
    return config.when(
      loading: () => const _Splash(),
      error: (error, _) => _Offline(message: '$error', onRetry: () => ref.invalidate(appConfigProvider)),
      data: (data) => MaterialApp(
        title: data.appName,
        debugShowCheckedModeBanner: false,
        theme: TenantTheme.fromTokens(data.tokens),
        darkTheme: TenantTheme.fromTokens(data.tokens, brightness: Brightness.dark),
        // A forced update or a maintenance window replaces the whole app, not just a banner:
        // there is no point letting someone browse a shop that cannot take their order.
        home: data.update.mustUpdate || data.maintenance.on
            ? GateScreen(config: data)
            : const HomeScreen(),
      ),
    );
  }
}

class _Splash extends StatelessWidget {
  const _Splash();

  @override
  Widget build(BuildContext context) => const MaterialApp(
        home: Scaffold(body: Center(child: CircularProgressIndicator())),
      );
}

class _Offline extends StatelessWidget {
  const _Offline({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => MaterialApp(
        home: Scaffold(
          body: Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Text('We could not reach the store.', textAlign: TextAlign.center),
                  const SizedBox(height: 12),
                  FilledButton(onPressed: onRetry, child: const Text('Try again')),
                ],
              ),
            ),
          ),
        ),
      );
}
