import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/api_client.dart';
import '../api/endpoints.dart';
import '../models/money.dart';

class CartLine {
  CartLine({
    required this.variantId,
    required this.title,
    required this.qty,
    required this.lineTotal,
    this.imageUrl,
  });

  factory CartLine.fromJson(Map<String, dynamic> json) => CartLine(
        variantId: json['variant_id'] as String,
        title: (json['title'] ?? json['title_en'] ?? '') as String,
        qty: json['qty'] as int? ?? 1,
        lineTotal: Money.parse(json['line_total']),
        imageUrl: json['image_url'] as String?,
      );

  final String variantId;
  final String title;
  final int qty;
  final Money lineTotal;
  final String? imageUrl;
}

class CartState {
  const CartState({this.lines = const [], this.total = const Money('0.00'), this.loading = false});

  final List<CartLine> lines;
  final Money total;
  final bool loading;

  int get count => lines.fold(0, (sum, line) => sum + line.qty);

  CartState copyWith({List<CartLine>? lines, Money? total, bool? loading}) =>
      CartState(lines: lines ?? this.lines, total: total ?? this.total, loading: loading ?? this.loading);
}

/// The cart lives on the server, so it survives a reinstall and matches what checkout will charge.
/// The controller keeps a local copy only so the badge and the list can render instantly.
class CartController extends StateNotifier<CartState> {
  CartController(this._api) : super(const CartState());

  final ApiClient _api;

  Future<void> load() async {
    state = state.copyWith(loading: true);
    try {
      final data = await _api.get(Endpoints.cart);
      final lines = ((data['items'] ?? []) as List)
          .map((item) => CartLine.fromJson(Map<String, dynamic>.from(item as Map)))
          .toList();
      state = CartState(lines: lines, total: Money.parse(data['subtotal'] ?? '0.00'));
    } finally {
      state = state.copyWith(loading: false);
    }
  }

  Future<void> add(String variantId, {int qty = 1}) async {
    await _api.post(Endpoints.cartItems, body: {'variant_id': variantId, 'qty': qty});
    await load();
  }

  /// qty 0 removes the line; the server owns that rule, the app just says what the buyer did.
  Future<void> setQty(String variantId, int qty) async {
    await _api.patch('${Endpoints.cartItems}/$variantId', body: {'qty': qty < 0 ? 0 : qty});
    await load();
  }
}

/// The app overrides `apiClientProvider` at startup; everything else hangs off it.
final apiClientProvider = Provider<ApiClient>((ref) {
  throw UnimplementedError('Override apiClientProvider in the app ProviderScope');
});

final cartControllerProvider =
    StateNotifierProvider<CartController, CartState>((ref) => CartController(ref.watch(apiClientProvider)));
