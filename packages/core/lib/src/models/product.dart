import 'money.dart';

class ProductCard {
  ProductCard({
    required this.id,
    required this.slug,
    required this.title,
    required this.price,
    required this.inStock,
    this.imageUrl,
    this.vendorName,
    this.ratingAvg,
    this.ratingCount,
  });

  factory ProductCard.fromJson(Map<String, dynamic> json) {
    final image = json['image'];
    String? url;
    if (image is Map) {
      final jpeg = image['jpeg'];
      if (jpeg is Map && jpeg.isNotEmpty) url = jpeg.values.first as String?;
    }
    return ProductCard(
      id: json['id'] as String,
      slug: json['slug'] as String,
      title: (json['title_en'] ?? json['title'] ?? '') as String,
      price: Money.parse(json['min_price']),
      inStock: json['in_stock'] as bool? ?? true,
      imageUrl: url ?? json['image_url'] as String?,
      vendorName: json['vendor_name'] as String?,
      ratingAvg: (json['rating_avg'] as num?)?.toDouble(),
      ratingCount: json['rating_count'] as int?,
    );
  }

  final String id;
  final String slug;
  final String title;
  final Money price;
  final bool inStock;
  final String? imageUrl;
  final String? vendorName;
  final double? ratingAvg;
  final int? ratingCount;
}

class Variant {
  Variant({
    required this.id,
    required this.sku,
    required this.price,
    required this.available,
    required this.options,
  });

  factory Variant.fromJson(Map<String, dynamic> json) => Variant(
        id: json['id'] as String,
        sku: json['sku'] as String,
        price: Money.parse(json['price']),
        available: json['available'] as int? ?? 0,
        options: Map<String, String>.from(json['options'] as Map? ?? {}),
      );

  final String id;
  final String sku;
  final Money price;
  final int available;
  final Map<String, String> options;

  String get label => options.values.isEmpty ? sku : options.values.join(' / ');
}
