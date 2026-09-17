import 'package:flutter/material.dart';
import 'package:marketplace_core/marketplace_core.dart';

import 'product_tile.dart';

/// One renderer per section type the page builder can publish. An unknown type renders nothing —
/// an older app must degrade quietly when the tenant publishes a section it has never heard of.
class SectionView extends StatelessWidget {
  const SectionView({super.key, required this.section});

  final Section section;

  @override
  Widget build(BuildContext context) {
    switch (section.type) {
      case 'hero':
      case 'banner':
        return _Banner(section: section);
      case 'product_grid':
      case 'featured_products':
      case 'new_arrivals':
      case 'best_sellers':
        return _ProductRow(section: section);
      case 'categories':
        return _Categories(section: section);
      case 'rich_text':
        return Padding(
          padding: const EdgeInsets.all(16),
          child: Text(section.settings['body'] as String? ?? ''),
        );
      default:
        return const SizedBox.shrink();
    }
  }
}

class _Banner extends StatelessWidget {
  const _Banner({required this.section});

  final Section section;

  @override
  Widget build(BuildContext context) {
    final image = section.settings['image_url'] as String?;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: AspectRatio(
          aspectRatio: 16 / 9,
          child: image == null
              ? Container(color: Theme.of(context).colorScheme.surfaceContainerHighest)
              : Image.network(image, fit: BoxFit.cover),
        ),
      ),
    );
  }
}

class _ProductRow extends StatelessWidget {
  const _ProductRow({required this.section});

  final Section section;

  @override
  Widget build(BuildContext context) {
    final products = section.items
        .map((item) => ProductCard.fromJson(Map<String, dynamic>.from(item as Map)))
        .toList();
    if (products.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (section.title != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
            child: Text(section.title!, style: Theme.of(context).textTheme.titleMedium),
          ),
        SizedBox(
          height: 268,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 16),
            itemCount: products.length,
            separatorBuilder: (_, __) => const SizedBox(width: 12),
            itemBuilder: (context, index) => SizedBox(
              width: 160,
              child: ProductTile(product: products[index]),
            ),
          ),
        ),
      ],
    );
  }
}

class _Categories extends StatelessWidget {
  const _Categories({required this.section});

  final Section section;

  @override
  Widget build(BuildContext context) {
    final items = section.items;
    if (items.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.all(16),
      child: Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          for (final item in items)
            ActionChip(
              label: Text((item as Map)['name_en'] as String? ?? ''),
              onPressed: () {},
            ),
        ],
      ),
    );
  }
}
