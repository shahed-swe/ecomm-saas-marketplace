import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import '../providers.dart';

/// What to pack today. Booking a courier is one button, because that is the whole job on a phone.
class VendorOrdersScreen extends ConsumerStatefulWidget {
  const VendorOrdersScreen({super.key});

  @override
  ConsumerState<VendorOrdersScreen> createState() => _VendorOrdersScreenState();
}

class _VendorOrdersScreenState extends ConsumerState<VendorOrdersScreen> {
  String? _busyId;

  Future<void> _act(String subOrderId, Future<void> Function() action) async {
    setState(() => _busyId = subOrderId);
    try {
      await action();
      ref.invalidate(vendorOrdersProvider);
    } on ApiError catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.detail)));
      }
    } finally {
      if (mounted) setState(() => _busyId = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final api = ref.watch(apiProvider);
    final orders = ref.watch(vendorOrdersProvider(null));
    return Scaffold(
      appBar: AppBar(title: const Text('Orders')),
      body: RefreshIndicator(
        onRefresh: () async => ref.invalidate(vendorOrdersProvider),
        child: orders.when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (error, _) => Center(child: Text('$error')),
          data: (rows) => rows.isEmpty
              ? const Center(child: Text('Nothing to pack right now.'))
              : ListView.separated(
                  itemCount: rows.length,
                  separatorBuilder: (_, __) => const Divider(height: 1),
                  itemBuilder: (context, index) {
                    final order = Map<String, dynamic>.from(rows[index] as Map);
                    final id = order['id'] as String;
                    final status = order['status'] as String? ?? '';
                    final busy = _busyId == id;
                    return ListTile(
                      title: Text(order['number'] as String? ?? ''),
                      subtitle: Text(
                        '${status.replaceAll('_', ' ')} · ${Money.parse(order['total']).format()}'
                        '${order['payment_method'] == 'cod' ? ' · COD' : ''}',
                      ),
                      trailing: busy
                          ? const SizedBox(
                              width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                          : switch (status) {
                              'confirmed' => FilledButton(
                                  onPressed: () => _act(
                                    id,
                                    () => api.post(VendorEndpoints.ready(id)).then((_) {}),
                                  ),
                                  child: const Text('Packed'),
                                ),
                              'processing' => FilledButton(
                                  onPressed: () => _act(
                                    id,
                                    () => api.post(VendorEndpoints.ship(id), body: {}).then((_) {}),
                                  ),
                                  child: const Text('Book courier'),
                                ),
                              _ => const SizedBox.shrink(),
                            },
                    );
                  },
                ),
        ),
      ),
    );
  }
}
