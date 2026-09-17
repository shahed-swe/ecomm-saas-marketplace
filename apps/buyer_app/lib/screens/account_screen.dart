import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:marketplace_core/marketplace_core.dart';

import '../providers.dart';

/// Account, including the delete route both app stores require. The wording is deliberately plain:
/// a person deleting their account deserves to know exactly what survives and why.
class AccountScreen extends ConsumerStatefulWidget {
  const AccountScreen({super.key});

  @override
  ConsumerState<AccountScreen> createState() => _AccountScreenState();
}

class _AccountScreenState extends ConsumerState<AccountScreen> {
  Map<String, dynamic>? _deletion;
  String? _error;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    final status = await ref.read(apiProvider).get(Endpoints.accountDelete);
    if (mounted) setState(() => _deletion = status);
  }

  Future<void> _requestDeletion() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete your account?'),
        content: const Text(
          'Your profile, addresses and saved devices are removed after 14 days. '
          'Past orders stay in the shop\'s records, with your details removed, because the shop '
          'must keep sales records for tax.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Keep it')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      await ref.read(apiProvider).post(Endpoints.accountDelete, body: {});
      await _refresh();
    } on ApiError catch (error) {
      setState(() => _error = error.detail);
    }
  }

  @override
  Widget build(BuildContext context) {
    final pending = _deletion?['status'] == 'pending';
    return Scaffold(
      appBar: AppBar(title: const Text('Account')),
      body: ListView(
        children: [
          ListTile(
            title: const Text('Notifications'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () {},
          ),
          ListTile(
            title: const Text('Store credit'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () {},
          ),
          const Divider(),
          if (pending)
            ListTile(
              title: const Text('Deletion scheduled'),
              subtitle: Text('On ${_deletion?['scheduled_for'] ?? ''}'),
              trailing: TextButton(
                onPressed: () async {
                  await ref.read(apiProvider).post('${Endpoints.accountDelete}/cancel');
                  await _refresh();
                },
                child: const Text('Cancel'),
              ),
            )
          else
            ListTile(
              title: Text('Delete my account',
                  style: TextStyle(color: Theme.of(context).colorScheme.error)),
              onTap: _requestDeletion,
            ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.all(16),
              child: Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
            ),
          ListTile(
            title: const Text('Sign out'),
            onTap: () => ref.read(sessionProvider).signOut(),
          ),
        ],
      ),
    );
  }
}
