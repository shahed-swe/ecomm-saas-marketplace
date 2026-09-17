import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import 'checkout_screen.dart';

class CartScreen extends ConsumerWidget {
  const CartScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final cart = ref.watch(cartControllerProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Cart')),
      body: cart.lines.isEmpty
          ? const Center(child: Text('Your cart is empty.'))
          : ListView.builder(
              itemCount: cart.lines.length,
              itemBuilder: (context, index) {
                final line = cart.lines[index];
                return ListTile(
                  title: Text(line.title),
                  subtitle: Text(line.lineTotal.format()),
                  trailing: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      IconButton(
                        icon: const Icon(Icons.remove),
                        onPressed: () => ref
                            .read(cartControllerProvider.notifier)
                            .setQty(line.variantId, line.qty - 1),
                      ),
                      Text('${line.qty}'),
                      IconButton(
                        icon: const Icon(Icons.add),
                        onPressed: () => ref
                            .read(cartControllerProvider.notifier)
                            .setQty(line.variantId, line.qty + 1),
                      ),
                    ],
                  ),
                );
              },
            ),
      bottomNavigationBar: cart.lines.isEmpty
          ? null
          : SafeArea(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: FilledButton(
                  onPressed: () => Navigator.of(context).push(
                    MaterialPageRoute<void>(builder: (_) => const CheckoutScreen()),
                  ),
                  child: Text('Checkout · ${cart.total.format()}'),
                ),
              ),
            ),
    );
  }
}
