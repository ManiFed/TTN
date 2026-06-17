import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';
import 'target_detail_screen.dart';

/// Recent photometric measurements across the member's telescopes.
class ObservationsTab extends StatefulWidget {
  const ObservationsTab({super.key});

  @override
  State<ObservationsTab> createState() => _ObservationsTabState();
}

class _ObservationsTabState extends State<ObservationsTab> {
  late Future<List<Observation>> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<List<Observation>> _load() {
    final state = context.read<AppState>();
    return state.api.observations().catchError((e) {
      state.handleAuthError(e);
      throw e;
    });
  }

  Future<void> _refresh() async => setState(() => _future = _load());

  @override
  Widget build(BuildContext context) {
    return AsyncView<List<Observation>>(
      future: _future,
      onRefresh: _refresh,
      isEmpty: (list) => list.isEmpty,
      emptyMessage: 'No observations yet.\nThey appear here after a clear night.',
      builder: (context, obs) => ListView.builder(
        padding: const EdgeInsets.all(16),
        itemCount: obs.length,
        itemBuilder: (context, i) => _ObservationCard(obs: obs[i]),
      ),
    );
  }
}

class _ObservationCard extends StatelessWidget {
  const _ObservationCard({required this.obs});
  final Observation obs;

  @override
  Widget build(BuildContext context) {
    final submitted = obs.aavsoSubmitted;
    final target = obs.targetName.isEmpty ? 'Unknown target' : obs.targetName;
    return Card(
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        onTap: obs.targetName.isEmpty
            ? null
            : () => Navigator.push(
                  context,
                  MaterialPageRoute<void>(
                    builder: (_) =>
                        TargetDetailScreen(targetName: obs.targetName),
                  ),
                ),
        title: Text(target, style: Theme.of(context).textTheme.titleMedium),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Text(
            'Magnitude ${obs.magnitude.toStringAsFixed(3)} '
            '± ${obs.uncertainty.toStringAsFixed(3)}'
            '${obs.filter.isNotEmpty ? '  ·  ${obs.filter.toUpperCase()}' : ''}',
            style: Theme.of(context).textTheme.bodyMedium,
          ),
        ),
        trailing: Semantics(
          label: submitted ? 'Submitted to AAVSO' : 'Pending submission',
          child: Tooltip(
            message: submitted ? 'Submitted to AAVSO' : 'Pending',
            child: Icon(
              submitted ? Icons.verified : Icons.schedule,
              color: submitted ? BSTheme.success : BSTheme.warning,
              size: 26,
            ),
          ),
        ),
      ),
    );
  }
}
