import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';
import 'package:url_launcher/url_launcher.dart';

import '../providers.dart';
import 'orders_screen.dart';

/// Checkout on a phone: one screen, the quote refreshed from the server, and an idempotency key on
/// the placement so a flaky mobile connection cannot produce two orders.
class CheckoutScreen extends ConsumerStatefulWidget {
  const CheckoutScreen({super.key});

  @override
  ConsumerState<CheckoutScreen> createState() => _CheckoutScreenState();
}

class _CheckoutScreenState extends ConsumerState<CheckoutScreen> {
  Map<String, dynamic>? _quote;
  List<dynamic> _addresses = const [];
  String? _addressId;
  String _method = 'cod';
  bool _useCredit = false;
  bool _busy = false;
  String? _error;

  /// Created once for this screen: if the network drops mid-request and the buyer taps again, the
  /// server recognises the same key and returns the order it already made instead of a second one.
  late final String _idempotencyKey =
      '${DateTime.now().microsecondsSinceEpoch.toRadixString(36)}'
      '${Random().nextInt(0x7fffffff).toRadixString(36)}';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final api = ref.read(apiProvider);
    final addresses = await api.getList(Endpoints.addresses);
    final district = addresses.isEmpty
        ? 'dhaka'
        : (addresses.first as Map)['district_code'] as String? ?? 'dhaka';
    final quote = await api.post(Endpoints.quote, body: {'district_code': district});
    if (!mounted) return;
    setState(() {
      _addresses = addresses;
      _addressId = addresses.isEmpty ? null : (addresses.first as Map)['id'] as String?;
      _quote = quote;
      final methods = List<String>.from(quote['payment_methods'] as List? ?? const []);
      _method = methods.contains('cod') ? 'cod' : (methods.isEmpty ? 'cod' : methods.first);
    });
  }

  Future<void> _place() async {
    final quote = _quote;
    if (quote == null || _addressId == null) return;
    setState(() { _busy = true; _error = null; });
    final api = ref.read(apiProvider);
    try {
      final order = await api.post(
        Endpoints.place,
        idempotencyKey: _idempotencyKey,
        body: {
          'address_id': _addressId,
          'payment_method': _method,
          'expected_total': quote['grand_total'],
          'use_store_credit': _useCredit && _method != 'cod',
        },
      );
      final due = Money.parse(order['amount_due'] ?? '0');
      if (_method != 'cod' && !due.isZero) {
        final payment = await api.post(
          Endpoints.startPayment(),
          body: {'order_number': order['number']},
        );
        final url = payment['redirect_url'] as String?;
        if (url != null) {
          await launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication);
        }
      }
      await ref.read(cartControllerProvider.notifier).load();
      if (!mounted) return;
      Navigator.of(context).pushReplacement(
        MaterialPageRoute<void>(builder: (_) => const OrdersScreen()),
      );
    } on ApiError catch (error) {
      setState(() => _error = error.detail);
      if (error.code == 'price_changed') await _load();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final quote = _quote;
    final credit = Money.parse(quote?['store_credit_available'] ?? '0');
    final methods = List<String>.from(quote?['payment_methods'] as List? ?? const []);
    return Scaffold(
      appBar: AppBar(title: const Text('Checkout')),
      body: quote == null
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Text('Deliver to', style: Theme.of(context).textTheme.titleSmall),
                for (final address in _addresses)
                  RadioListTile<String>(
                    value: (address as Map)['id'] as String,
                    groupValue: _addressId,
                    onChanged: (value) => setState(() => _addressId = value),
                    title: Text(address['recipient_name'] as String? ?? ''),
                    subtitle: Text(
                      '${address['address_line']}, ${address['upazila'] ?? ''} · ${address['district_code']}',
                    ),
                  ),
                const Divider(),
                Text('Payment', style: Theme.of(context).textTheme.titleSmall),
                for (final method in methods)
                  RadioListTile<String>(
                    value: method,
                    groupValue: _method,
                    onChanged: (value) => setState(() => _method = value!),
                    title: Text(switch (method) {
                      'cod' => 'Cash on delivery',
                      'bkash' => 'bKash',
                      _ => 'Card / SSLCommerz',
                    }),
                  ),
                if (!credit.isZero && _method != 'cod')
                  CheckboxListTile(
                    value: _useCredit,
                    onChanged: (value) => setState(() => _useCredit = value ?? false),
                    title: Text('Use store credit (${credit.format()})'),
                  ),
                const Divider(),
                _Row(label: 'Items', value: Money.parse(quote['items_subtotal']).format()),
                _Row(label: 'Delivery', value: Money.parse(quote['shipping_total']).format()),
                _Row(label: 'VAT', value: Money.parse(quote['vat_total']).format()),
                _Row(label: 'Total', value: Money.parse(quote['grand_total']).format(), bold: true),
                if (_error != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
                  ),
              ],
            ),
      bottomNavigationBar: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: FilledButton(
            onPressed: _busy || quote == null || _addressId == null ? null : _place,
            child: Text(_busy ? 'Placing…' : 'Place order'),
          ),
        ),
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value, this.bold = false});

  final String label;
  final String value;
  final bool bold;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(label),
            Text(value, style: TextStyle(fontWeight: bold ? FontWeight.w700 : FontWeight.w400)),
          ],
        ),
      );
}
