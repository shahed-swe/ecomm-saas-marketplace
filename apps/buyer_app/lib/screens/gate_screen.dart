import 'package:flutter/material.dart';
import 'package:marketplace_core/marketplace_core.dart';
import 'package:url_launcher/url_launcher.dart';

/// Shown instead of the app when it is too old to be safe, or the store is closed for maintenance.
/// Deliberately a dead end: there is no "continue anyway", because an app below the minimum version
/// may be talking to endpoints that no longer behave the way it expects.
class GateScreen extends StatelessWidget {
  const GateScreen({super.key, required this.config});

  final AppConfig config;

  @override
  Widget build(BuildContext context) {
    final maintenance = config.maintenance.on;
    final message = maintenance
        ? (config.maintenance.message ?? 'We are back shortly.')
        : (config.update.message ?? 'Please update to keep shopping.');
    return Scaffold(
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(maintenance ? Icons.construction : Icons.system_update, size: 56),
              const SizedBox(height: 16),
              Text(
                maintenance ? config.storeName : 'Update ${config.appName}',
                style: Theme.of(context).textTheme.titleLarge,
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 8),
              Text(message, textAlign: TextAlign.center),
              const SizedBox(height: 24),
              if (!maintenance && config.update.storeUrl != null)
                FilledButton(
                  onPressed: () => launchUrl(
                    Uri.parse(config.update.storeUrl!),
                    mode: LaunchMode.externalApplication,
                  ),
                  child: const Text('Update now'),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
