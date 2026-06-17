import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';

/// "Tonight" overview: a friendly greeting plus the member's cumulative impact.
class DashboardTab extends StatefulWidget {
  const DashboardTab({super.key});

  @override
  State<DashboardTab> createState() => _DashboardTabState();
}

class _DashboardTabState extends State<DashboardTab> {
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
    final name = context.select<AppState, String>(
      (s) => s.member?.displayName ?? 'stargazer',
    );
    return AsyncView<MemberStats>(
      future: _future,
      onRefresh: _refresh,
      builder: (context, stats) => ListView(
        padding: const EdgeInsets.all(16),
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
          const SizedBox(height: 20),
          GridView.count(
            crossAxisCount: 2,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            childAspectRatio: 1.25,
            mainAxisSpacing: 12,
            crossAxisSpacing: 12,
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
        ],
      ),
    );
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
    return Semantics(
      label: '$value $label',
      child: Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Icon(icon, color: color, size: 30),
              const Spacer(),
              Text('$value',
                  style: Theme.of(context)
                      .textTheme
                      .displaySmall
                      ?.copyWith(fontWeight: FontWeight.w800)),
              Text(label, style: Theme.of(context).textTheme.bodyMedium),
            ],
          ),
        ),
      ),
    );
  }
}
