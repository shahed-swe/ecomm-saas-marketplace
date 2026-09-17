import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import '../providers.dart';
import '../widgets/section_view.dart';
import 'cart_screen.dart';
import 'orders_screen.dart';
import 'search_screen.dart';

/// The home screen renders whatever the tenant arranged in the page builder. Adding a banner or a
/// new "Eid picks" row is a publish on the web, not an app release.
class HomeScreen extends ConsumerWidget {
  const HomeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final sections = ref.watch(homeSectionsProvider);
    final config = ref.watch(appConfigProvider).valueOrNull;
    final cart = ref.watch(cartControllerProvider);
    return Scaffold(
      appBar: AppBar(
        title: Text(config?.appName ?? ''),
        actions: [
          IconButton(
            icon: const Icon(Icons.search),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => const SearchScreen()),
            ),
          ),
          IconButton(
            icon: Badge(
              isLabelVisible: cart.count > 0,
              label: Text('${cart.count}'),
              child: const Icon(Icons.shopping_bag_outlined),
            ),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => const CartScreen()),
            ),
          ),
          IconButton(
            icon: const Icon(Icons.receipt_long_outlined),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => const OrdersScreen()),
            ),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(homeSectionsProvider);
          await ref.read(cartControllerProvider.notifier).load();
        },
        child: sections.when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (error, _) => ListView(
            children: [
              const SizedBox(height: 120),
              Center(child: Text('$error', textAlign: TextAlign.center)),
            ],
          ),
          data: (data) => ListView.builder(
            padding: const EdgeInsets.symmetric(vertical: 12),
            itemCount: data.length,
            itemBuilder: (context, index) => SectionView(section: data[index]),
          ),
        ),
      ),
    );
  }
}
