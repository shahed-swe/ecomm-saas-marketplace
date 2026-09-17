import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import '../providers.dart';
import '../widgets/product_tile.dart';

class SearchScreen extends ConsumerStatefulWidget {
  const SearchScreen({super.key});

  @override
  ConsumerState<SearchScreen> createState() => _SearchScreenState();
}

class _SearchScreenState extends ConsumerState<SearchScreen> {
  final _controller = TextEditingController();
  List<ProductCard> _results = const [];
  bool _searching = false;

  Future<void> _run(String query) async {
    if (query.trim().isEmpty) return;
    setState(() => _searching = true);
    try {
      final data = await ref.read(apiProvider).get(Endpoints.search, query: {'q': query});
      setState(() {
        _results = ((data['items'] ?? data['results'] ?? []) as List)
            .map((item) => ProductCard.fromJson(Map<String, dynamic>.from(item as Map)))
            .toList();
      });
    } finally {
      if (mounted) setState(() => _searching = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: TextField(
          controller: _controller,
          autofocus: true,
          textInputAction: TextInputAction.search,
          // Banglish works: the API expands "jama" and "জামা" to the same results.
          decoration: const InputDecoration(hintText: 'Search products', border: InputBorder.none),
          onSubmitted: _run,
        ),
      ),
      body: _searching
          ? const Center(child: CircularProgressIndicator())
          : GridView.builder(
              padding: const EdgeInsets.all(16),
              gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: 2,
                mainAxisSpacing: 12,
                crossAxisSpacing: 12,
                childAspectRatio: 0.62,
              ),
              itemCount: _results.length,
              itemBuilder: (context, index) => ProductTile(product: _results[index]),
            ),
    );
  }
}
