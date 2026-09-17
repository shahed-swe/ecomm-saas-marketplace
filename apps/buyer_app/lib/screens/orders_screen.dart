import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import '../providers.dart';

final ordersProvider = FutureProvider<List<OrderSummary>>((ref) async {
  final rows = await ref.watch(apiProvider).getList(Endpoints.orders);
  return rows
      .map((row) => OrderSummary.fromJson(Map<String, dynamic>.from(row as Map)))
      .toList();
});

class OrdersScreen extends ConsumerWidget {
  const OrdersScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final orders = ref.watch(ordersProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('My orders')),
      body: orders.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => Center(child: Text('$error')),
        data: (data) => data.isEmpty
            ? const Center(child: Text('No orders yet.'))
            : ListView.separated(
                itemCount: data.length,
                separatorBuilder: (_, __) => const Divider(height: 1),
                itemBuilder: (context, index) {
                  final order = data[index];
                  return ListTile(
                    title: Text(order.number),
                    subtitle: Text(
                      '${order.status.replaceAll('_', ' ')} · ${order.shipments.length} shipment(s)',
                    ),
                    trailing: Text(order.total.format()),
                    onTap: () => Navigator.of(context).push(
                      MaterialPageRoute<void>(
                        builder: (_) => OrderScreen(number: order.number),
                      ),
                    ),
                  );
                },
              ),
      ),
    );
  }
}

class OrderScreen extends ConsumerWidget {
  const OrderScreen({super.key, required this.number});

  final String number;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final api = ref.watch(apiProvider);
    return Scaffold(
      appBar: AppBar(title: Text(number)),
      body: FutureBuilder<List<dynamic>>(
        future: api.getList(Endpoints.tracking(number)),
        builder: (context, snapshot) {
          final parcels = snapshot.data ?? const [];
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              for (final parcel in parcels)
                Card(
                  child: ListTile(
                    title: Text((parcel as Map)['shipment_number'] as String? ?? ''),
                    subtitle: Text(
                      '${parcel['courier'] ?? ''} · ${(parcel['status'] as String? ?? '').replaceAll('_', ' ')}',
                    ),
                    trailing: parcel['tracking_code'] == null
                        ? null
                        : Text(parcel['tracking_code'] as String),
                  ),
                ),
              if (parcels.isEmpty) const Text('Your parcel has not been booked yet.'),
            ],
          );
        },
      ),
    );
  }
}
