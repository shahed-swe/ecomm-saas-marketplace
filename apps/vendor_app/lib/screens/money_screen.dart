import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import '../providers.dart';

/// What the seller is owed, what is held back and why — the same numbers the tenant sees.
class MoneyScreen extends ConsumerWidget {
  const MoneyScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final money = ref.watch(vendorMoneyProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Money')),
      body: money.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => Center(child: Text('$error')),
        data: (data) {
          final balance = Map<String, dynamic>.from(data['balance'] as Map);
          final payouts = (data['payouts'] ?? []) as List;
          final score = Map<String, dynamic>.from(data['scorecard'] as Map);
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('Available now', style: Theme.of(context).textTheme.bodySmall),
                      Text(
                        Money.parse(balance['available']).format(),
                        style: Theme.of(context).textTheme.headlineSmall,
                      ),
                      const SizedBox(height: 8),
                      Text('Total owed: ${Money.parse(balance['payable']).format()}'),
                      Text('Held for returns: ${Money.parse(balance['reserve']).format()}'),
                      if (balance['on_hold'] == true)
                        Text(
                          'A payout hold is active — support can tell you why.',
                          style: TextStyle(color: Theme.of(context).colorScheme.error),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 16),
              Text('Scorecard', style: Theme.of(context).textTheme.titleMedium),
              Text('${score['score']} · ${score['band']} · ${score['orders']} orders'),
              const SizedBox(height: 16),
              Text('Payouts', style: Theme.of(context).textTheme.titleMedium),
              for (final payout in payouts)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text('${(payout as Map)['period_end']}'),
                  subtitle: Text(
                    '${payout['status']}${payout['reference'] == null ? '' : ' · ${payout['reference']}'}',
                  ),
                  trailing: Text(Money.parse(payout['net']).format()),
                ),
              if (payouts.isEmpty) const Text('No payouts yet.'),
            ],
          );
        },
      ),
    );
  }
}
