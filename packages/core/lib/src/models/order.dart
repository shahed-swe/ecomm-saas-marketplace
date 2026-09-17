import 'money.dart';

class OrderSummary {
  OrderSummary({
    required this.number,
    required this.status,
    required this.total,
    required this.placedAt,
    required this.paymentMethod,
    required this.shipments,
  });

  factory OrderSummary.fromJson(Map<String, dynamic> json) => OrderSummary(
        number: json['number'] as String,
        status: json['status'] as String? ?? 'unknown',
        total: Money.parse(json['grand_total']),
        placedAt: DateTime.tryParse(json['placed_at'] as String? ?? '') ?? DateTime.now(),
        paymentMethod: json['payment_method'] as String? ?? 'cod',
        shipments: ((json['shipments'] ?? []) as List)
            .map((s) => Shipment.fromJson(Map<String, dynamic>.from(s as Map)))
            .toList(),
      );

  final String number;
  final String status;
  final Money total;
  final DateTime placedAt;
  final String paymentMethod;
  final List<Shipment> shipments;

  bool get awaitingPayment => status == 'pending_payment' && paymentMethod != 'cod';
}

class Shipment {
  Shipment({required this.number, required this.status, required this.total, this.vendorName});

  factory Shipment.fromJson(Map<String, dynamic> json) => Shipment(
        number: json['number'] as String? ?? '',
        status: json['status'] as String? ?? '',
        total: Money.parse(json['total']),
        vendorName: json['vendor_name'] as String?,
      );

  final String number;
  final String status;
  final Money total;
  final String? vendorName;
}
