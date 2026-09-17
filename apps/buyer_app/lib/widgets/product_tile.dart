import 'package:flutter/material.dart';
import 'package:marketplace_core/marketplace_core.dart';

import '../screens/product_screen.dart';

class ProductTile extends StatelessWidget {
  const ProductTile({super.key, required this.product});

  final ProductCard product;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: () => Navigator.of(context).push(
        MaterialPageRoute<void>(builder: (_) => ProductScreen(slug: product.slug)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ClipRRect(
            borderRadius: BorderRadius.circular(10),
            child: AspectRatio(
              aspectRatio: 1,
              child: product.imageUrl == null
                  ? Container(color: Theme.of(context).colorScheme.surfaceContainerHighest)
                  : Image.network(product.imageUrl!, fit: BoxFit.cover),
            ),
          ),
          const SizedBox(height: 6),
          Text(product.title, maxLines: 2, overflow: TextOverflow.ellipsis),
          Text(product.price.format(), style: const TextStyle(fontWeight: FontWeight.w600)),
          if ((product.ratingCount ?? 0) > 0)
            Text(
              '★ ${product.ratingAvg!.toStringAsFixed(1)} (${product.ratingCount})',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          if (!product.inStock)
            Text('Out of stock', style: TextStyle(color: Theme.of(context).colorScheme.error)),
        ],
      ),
    );
  }
}
