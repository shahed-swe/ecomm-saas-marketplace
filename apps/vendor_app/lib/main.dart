import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import 'providers.dart';
import 'screens/orders_screen.dart';
import 'screens/money_screen.dart';
import 'screens/returns_screen.dart';

void main() {
  runApp(
    ProviderScope(
      overrides: [apiClientProvider.overrideValue(buildApiClient())],
      child: const VendorApp(),
    ),
  );
}

class VendorApp extends ConsumerWidget {
  const VendorApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final config = ref.watch(appConfigProvider);
    return MaterialApp(
      title: config.valueOrNull?.appName ?? 'Seller',
      debugShowCheckedModeBanner: false,
      theme: TenantTheme.fromTokens(config.valueOrNull?.tokens ?? const {}),
      darkTheme: TenantTheme.fromTokens(
        config.valueOrNull?.tokens ?? const {},
        brightness: Brightness.dark,
      ),
      home: const VendorShell(),
    );
  }
}

/// A seller's day is three questions: what do I pack, what came back, and what am I owed.
class VendorShell extends StatefulWidget {
  const VendorShell({super.key});

  @override
  State<VendorShell> createState() => _VendorShellState();
}

class _VendorShellState extends State<VendorShell> {
  int _tab = 0;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _tab,
        children: const [VendorOrdersScreen(), ReturnsScreen(), MoneyScreen()],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (index) => setState(() => _tab = index),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.inventory_2_outlined), label: 'Orders'),
          NavigationDestination(icon: Icon(Icons.assignment_return_outlined), label: 'Returns'),
          NavigationDestination(icon: Icon(Icons.account_balance_wallet_outlined), label: 'Money'),
        ],
      ),
    );
  }
}
