import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import '../providers.dart';

/// Returns waiting on the seller: approve or refuse, then say what the goods were actually like.
class ReturnsScreen extends ConsumerWidget {
  const ReturnsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final api = ref.watch(apiProvider);
    final returns = ref.watch(vendorReturnsProvider);
    Future<void> act(Future<void> Function() action) async {
      try {
        await action();
        ref.invalidate(vendorReturnsProvider);
      } on ApiError catch (error) {
        if (context.mounted) {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.detail)));
        }
      }
    }

    return Scaffold(
      appBar: AppBar(title: const Text('Returns')),
      body: returns.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => Center(child: Text('$error')),
        data: (rows) => rows.isEmpty
            ? const Center(child: Text('No returns.'))
            : ListView.builder(
                itemCount: rows.length,
                itemBuilder: (context, index) {
                  final item = Map<String, dynamic>.from(rows[index] as Map);
                  final id = item['id'] as String;
                  final status = item['status'] as String? ?? '';
                  return Card(
                    margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text('${item['number']} · ${item['reason']}'),
                          Text(
                            '${status.replaceAll('_', ' ')} · '
                            '${Money.parse(item['refund_total']).format()} · '
                            'return postage: ${item['shipping_payer']}',
                            style: Theme.of(context).textTheme.bodySmall,
                          ),
                          const SizedBox(height: 8),
                          if (status == 'requested')
                            Row(
                              children: [
                                FilledButton(
                                  onPressed: () => act(() => api
                                      .post(VendorEndpoints.decision(id), body: {'approve': true})
                                      .then((_) {})),
                                  child: const Text('Approve'),
                                ),
                                const SizedBox(width: 8),
                                OutlinedButton(
                                  onPressed: () => act(() => api
                                      .post(VendorEndpoints.decision(id), body: {'approve': false})
                                      .then((_) {})),
                                  child: const Text('Refuse'),
                                ),
                              ],
                            ),
                          if (status == 'received')
                            Row(
                              children: [
                                FilledButton(
                                  onPressed: () => act(() => api
                                      .post(VendorEndpoints.qc(id), body: {'passed': true})
                                      .then((_) {})),
                                  child: const Text('Goods are fine'),
                                ),
                                const SizedBox(width: 8),
                                OutlinedButton(
                                  onPressed: () => act(() => api
                                      .post(VendorEndpoints.qc(id), body: {'passed': false})
                                      .then((_) {})),
                                  child: const Text('Not as claimed'),
                                ),
                              ],
                            ),
                        ],
                      ),
                    ),
                  );
                },
              ),
      ),
    );
  }
}
