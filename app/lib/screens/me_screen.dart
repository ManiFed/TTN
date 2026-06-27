import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';

/// Profile + cumulative-stats screen, pushed from the account menu.
class MeScreen extends StatefulWidget {
  const MeScreen({super.key});

  @override
  State<MeScreen> createState() => _MeScreenState();
}

class _MeScreenState extends State<MeScreen> {
  late Future<MemberStats> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<MemberStats> _load() {
    final state = context.read<AppState>();
    return state.api.stats().catchError((e) {
      state.handleAuthError(e);
      throw e;
    });
  }

  Future<void> _refresh() async => setState(() => _future = _load());

  @override
  Widget build(BuildContext context) {
    final member = context.select<AppState, Member?>((s) => s.member);
    final name = member?.displayName ?? 'stargazer';

    return Scaffold(
      appBar: AppBar(title: const Text('Me')),
      body: AsyncView<MemberStats>(
        future: _future,
        onRefresh: _refresh,
        builder: (context, stats) => ListView(
          padding: const EdgeInsets.fromLTRB(16, 20, 16, 24),
          children: [
            Text('Hello, $name',
                style: Theme.of(context).textTheme.headlineSmall),
            const SizedBox(height: 4),
            Text(
              stats.nodeCount == 0
                  ? 'Connect a telescope to start contributing to real science.'
                  : 'Your network has gathered real measurements for astronomers worldwide.',
              style: Theme.of(context).textTheme.bodyLarge,
            ),
            if (member != null && member.email.isNotEmpty) ...[
              const SizedBox(height: 8),
              Text(member.email,
                  style: Theme.of(context).textTheme.bodySmall),
            ],
            const SizedBox(height: 20),
            LayoutBuilder(
              builder: (context, constraints) {
                final cols = constraints.maxWidth > 500 ? 4 : 2;
                return GridView.count(
                  crossAxisCount: cols,
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  childAspectRatio: cols == 4 ? 1.8 : 1.6,
                  mainAxisSpacing: 8,
                  crossAxisSpacing: 8,
                  children: [
                    _StatCard(
                      label: 'Observations',
                      value: stats.totalObservations,
                      icon: Icons.camera_alt_outlined,
                      color: BSTheme.success,
                    ),
                    _StatCard(
                      label: 'Sent to AAVSO',
                      value: stats.aavsoSubmitted,
                      icon: Icons.send_outlined,
                      color: const Color(0xFF7DA9FF),
                    ),
                    _StatCard(
                      label: 'Stars watched',
                      value: stats.targetsObserved,
                      icon: Icons.star_outline,
                      color: const Color(0xFFFFC857),
                    ),
                    _StatCard(
                      label: 'Clear nights',
                      value: stats.clearNights,
                      icon: Icons.nights_stay_outlined,
                      color: BSTheme.warning,
                    ),
                  ],
                );
              },
            ),
            const SizedBox(height: 20),
            Card(
              child: ListTile(
                leading: const Icon(Icons.satellite_alt, size: 30),
                title: Text('${stats.nodeCount} '
                    'telescope${stats.nodeCount == 1 ? '' : 's'} connected'),
                subtitle: const Text('Tap the Telescopes tab to manage them'),
              ),
            ),
            const SizedBox(height: 32),
            const Divider(),
            const SizedBox(height: 8),
            TextButton.icon(
              onPressed: () => _confirmDeleteAccount(context),
              icon: const Icon(Icons.delete_forever_outlined,
                  color: Colors.redAccent),
              label: const Text(
                'Delete account',
                style: TextStyle(color: Colors.redAccent),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _confirmDeleteAccount(BuildContext context) async {
    final state = context.read<AppState>();
    final messenger = ScaffoldMessenger.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('Delete account?'),
        content: const Text(
          'This permanently deletes your account and all your data. '
          'Your telescopes will stop contributing to the network. '
          'This cannot be undone.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            style: TextButton.styleFrom(foregroundColor: Colors.redAccent),
            child: const Text('Delete my account'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    try {
      await state.deleteAccount();
    } catch (e) {
      if (!mounted) return;
      messenger.showSnackBar(
        SnackBar(content: Text('Could not delete account: $e')),
      );
    }
  }
}

class _StatCard extends StatelessWidget {
  const _StatCard({
    required this.label,
    required this.value,
    required this.icon,
    required this.color,
  });

  final String label;
  final int value;
  final IconData icon;
  final Color color;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    return Semantics(
      label: '$value $label',
      child: Card(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Row(
                children: [
                  Icon(icon, color: color, size: 18),
                  const SizedBox(width: 6),
                  Flexible(
                    child: Text(
                      label,
                      style: tt.bodySmall,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              Text(
                '$value',
                style: tt.headlineMedium?.copyWith(
                  fontWeight: FontWeight.w800,
                  color: color,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
