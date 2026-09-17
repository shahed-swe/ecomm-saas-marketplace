import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import '../providers.dart';

final productProvider = FutureProvider.family<Map<String, dynamic>, String>((ref, slug) {
  return ref.watch(apiProvider).get(Endpoints.product(slug));
});

class ProductScreen extends ConsumerStatefulWidget {
  const ProductScreen({super.key, required this.slug});

  final String slug;

  @override
  ConsumerState<ProductScreen> createState() => _ProductScreenState();
}

class _ProductScreenState extends ConsumerState<ProductScreen> {
  String? _variantId;
  bool _busy = false;

  @override
  Widget build(BuildContext context) {
    final product = ref.watch(productProvider(widget.slug));
    return Scaffold(
      appBar: AppBar(),
      body: product.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => Center(child: Text('$error')),
        data: (data) {
          final variants = ((data['variants'] ?? []) as List)
              .map((v) => Variant.fromJson(Map<String, dynamic>.from(v as Map)))
              .toList();
          final selected = variants.firstWhere(
            (v) => v.id == _variantId,
            orElse: () => variants.first,
          );
          final media = (data['media'] ?? []) as List;
          final image = media.isEmpty
              ? null
              : ((media.first as Map)['renditions'] as Map?)?['jpeg'] as Map?;
          return ListView(
            children: [
              if (image != null && image.isNotEmpty)
                AspectRatio(
                  aspectRatio: 1,
                  child: Image.network(image.values.first as String, fit: BoxFit.cover),
                ),
              Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(data['title_en'] as String? ?? '',
                        style: Theme.of(context).textTheme.titleLarge),
                    const SizedBox(height: 8),
                    Text(selected.price.format(),
                        style: Theme.of(context).textTheme.titleMedium),
                    const SizedBox(height: 12),
                    Wrap(
                      spacing: 8,
                      children: [
                        for (final variant in variants)
                          ChoiceChip(
                            label: Text(variant.label),
                            selected: variant.id == selected.id,
                            onSelected: variant.available > 0
                                ? (_) => setState(() => _variantId = variant.id)
                                : null,
                          ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    Text(data['description'] as String? ?? ''),
                  ],
                ),
              ),
            ],
          );
        },
      ),
      bottomNavigationBar: product.maybeWhen(
        data: (data) {
          final variants = ((data['variants'] ?? []) as List)
              .map((v) => Variant.fromJson(Map<String, dynamic>.from(v as Map)))
              .toList();
          final selected = variants.firstWhere(
            (v) => v.id == _variantId,
            orElse: () => variants.first,
          );
          return SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: FilledButton(
                onPressed: selected.available == 0 || _busy
                    ? null
                    : () async {
                        setState(() => _busy = true);
                        try {
                          await ref.read(cartControllerProvider.notifier).add(selected.id);
                          if (mounted) {
                            ScaffoldMessenger.of(context).showSnackBar(
                              const SnackBar(content: Text('Added to cart')),
                            );
                          }
                        } on ApiError catch (error) {
                          if (mounted) {
                            ScaffoldMessenger.of(context)
                                .showSnackBar(SnackBar(content: Text(error.detail)));
                          }
                        } finally {
                          if (mounted) setState(() => _busy = false);
                        }
                      },
                child: Text(selected.available == 0 ? 'Out of stock' : 'Add to cart'),
              ),
            ),
          );
        },
        orElse: () => const SizedBox.shrink(),
      ),
    );
  }
}
