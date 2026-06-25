import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';

/// Tonight's observation plan: active targets + member node status.
class DashboardTab extends StatefulWidget {
  const DashboardTab({super.key});

  @override
  State<DashboardTab> createState() => _DashboardTabState();
}

class _DashboardTabState extends State<DashboardTab> {
  late Future<_TonightData> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<_TonightData> _load() async {
    final state = context.read<AppState>();
    final results = await Future.wait([
      state.api.targets().catchError((e) {
        state.handleAuthError(e);
        throw e;
      }),
      state.api.nodes().catchError((e) {
        state.handleAuthError(e);
        throw e;
      }),
    ]);
    return _TonightData(
      targets: results[0] as List<Target>,
      nodes: results[1] as List<Node>,
    );
  }

  Future<void> _refresh() async => setState(() => _future = _load());

  @override
  Widget build(BuildContext context) {
    return AsyncView<_TonightData>(
      future: _future,
      onRefresh: _refresh,
      isEmpty: (d) => d.targets.isEmpty,
      emptyMessage: 'No targets scheduled for tonight.',
      builder: (context, data) => _TonightBody(data: data),
    );
  }
}

class _TonightData {
  final List<Target> targets;
  final List<Node> nodes;
  const _TonightData({required this.targets, required this.nodes});
}

class _TonightBody extends StatelessWidget {
  const _TonightBody({required this.data});
  final _TonightData data;

  @override
  Widget build(BuildContext context) {
    final now = DateTime.now();
    final dateStr =
        '${_weekday(now.weekday)}, ${_month(now.month)} ${now.day}';
    final onlineNodes = data.nodes.where((n) => n.online).length;

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 20, 16, 24),
      children: [
        // ── Night header ──────────────────────────────────────────────────
        Row(
          children: [
            const Icon(Icons.nightlight, size: 22, color: Color(0xFF7DA9FF)),
            const SizedBox(width: 8),
            Text(dateStr,
                style: Theme.of(context)
                    .textTheme
                    .titleMedium
                    ?.copyWith(color: const Color(0xFF7DA9FF))),
          ],
        ),
        const SizedBox(height: 4),

        // ── Node status summary ───────────────────────────────────────────
        if (data.nodes.isNotEmpty) ...[
          _NodeStatusBanner(nodes: data.nodes, onlineCount: onlineNodes),
          const SizedBox(height: 16),
        ],

        // ── Target list ───────────────────────────────────────────────────
        Text(
          'Tonight\'s targets',
          style: Theme.of(context).textTheme.titleSmall,
        ),
        const SizedBox(height: 8),
        ...data.targets.take(50).map((t) => _TargetRow(target: t)),
      ],
    );
  }

  String _weekday(int d) =>
      const ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d - 1];

  String _month(int m) => const [
        'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
      ][m - 1];
}

class _NodeStatusBanner extends StatelessWidget {
  const _NodeStatusBanner(
      {required this.nodes, required this.onlineCount});
  final List<Node> nodes;
  final int onlineCount;

  @override
  Widget build(BuildContext context) {
    final allOnline = onlineCount == nodes.length;
    final color = onlineCount > 0 ? BSTheme.success : BSTheme.danger;
    final label = nodes.length == 1
        ? (onlineCount == 1 ? 'Your telescope is online' : 'Your telescope is offline')
        : '$onlineCount of ${nodes.length} telescopes online';

    return Card(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(
          children: [
            Icon(
              allOnline ? Icons.check_circle : Icons.satellite_alt,
              color: color,
              size: 22,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Text(label,
                  style: Theme.of(context)
                      .textTheme
                      .bodyMedium
                      ?.copyWith(fontWeight: FontWeight.w600)),
            ),
          ],
        ),
      ),
    );
  }
}

class _TargetRow extends StatelessWidget {
  const _TargetRow({required this.target});
  final Target target;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final typeLabel = target.targetType.replaceAll('_', ' ');
    final magStr = target.mag != null
        ? '${target.mag!.toStringAsFixed(1)} ${target.magBand}'.trim()
        : '';

    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(
          children: [
            const Icon(Icons.star_outline,
                size: 22, color: Color(0xFFFFC857)),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(target.name,
                      style: tt.bodyMedium
                          ?.copyWith(fontWeight: FontWeight.w600)),
                  const SizedBox(height: 2),
                  Text(
                    [if (typeLabel.isNotEmpty) typeLabel, if (magStr.isNotEmpty) 'mag $magStr']
                        .join(' · '),
                    style: tt.bodySmall,
                  ),
                ],
              ),
            ),
            if (target.nMeasurements > 0)
              Semantics(
                label: '${target.nMeasurements} measurements',
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Text('${target.nMeasurements}',
                        style: tt.bodyMedium?.copyWith(
                            fontWeight: FontWeight.w700,
                            color: BSTheme.success)),
                    Text('obs', style: tt.bodySmall),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}
