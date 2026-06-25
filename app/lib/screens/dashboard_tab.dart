import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/glass.dart';

/// "Tonight" — live mission-control dashboard. No scrolling: a hero stat band
/// over the Aladin sky, then three floating glass panels that fill the screen.
class DashboardTab extends StatefulWidget {
  const DashboardTab({super.key});

  @override
  State<DashboardTab> createState() => _DashboardTabState();
}

// ── Data bundle ───────────────────────────────────────────────────────────────

class _DashboardData {
  const _DashboardData({
    required this.nodes,
    required this.recentObs,
    required this.targets,
    required this.alerts,
  });

  final List<Node> nodes;
  final List<Observation> recentObs;
  final List<Target> targets;
  final List<AppNotification> alerts;
}

// ── State ─────────────────────────────────────────────────────────────────────

class _DashboardTabState extends State<DashboardTab> {
  late Future<_DashboardData> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<_DashboardData> _load() async {
    final api = context.read<AppState>().api;

    // All four requests fire in parallel.
    final nodesFuture = api.nodes().catchError((_) => <Node>[]);
    final obsFuture =
        api.observations(days: 1, limit: 10).catchError((_) => <Observation>[]);
    final targetsFuture = api.targets().catchError((_) => <Target>[]);
    final notifsFuture = api.notifications(limit: 5);

    List<AppNotification> alerts;
    try {
      alerts = (await notifsFuture).$1;
    } catch (_) {
      alerts = [];
    }

    return _DashboardData(
      nodes: await nodesFuture,
      recentObs: await obsFuture,
      targets: await targetsFuture,
      alerts: alerts,
    );
  }

  Future<void> _refresh() async => setState(() => _future = _load());

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<_DashboardData>(
      future: _future,
      builder: (context, snap) {
        if (snap.connectionState == ConnectionState.waiting) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snap.hasError) {
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(32),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.cloud_off, size: 48, color: BSTheme.ink3),
                  const SizedBox(height: 12),
                  Text('${snap.error}',
                      textAlign: TextAlign.center,
                      style: const TextStyle(color: BSTheme.ink2)),
                  const SizedBox(height: 20),
                  ElevatedButton(onPressed: _refresh, child: const Text('Retry')),
                ],
              ),
            ),
          );
        }
        return _DashboardView(data: snap.data!, onRefresh: _refresh);
      },
    );
  }
}

// ── Dashboard view ────────────────────────────────────────────────────────────

class _DashboardView extends StatelessWidget {
  const _DashboardView({required this.data, required this.onRefresh});
  final _DashboardData data;
  final Future<void> Function() onRefresh;

  @override
  Widget build(BuildContext context) {
    final topPad = MediaQuery.of(context).padding.top + kToolbarHeight;
    final bottomPad = MediaQuery.of(context).padding.bottom + 64;
    final name = context.select<AppState, String>(
      (s) => s.member?.displayName ?? '',
    );

    final online = data.nodes.where((n) => n.online).length;
    final unread = data.alerts.where((a) => !a.read).length;

    return LayoutBuilder(
      builder: (context, constraints) => RefreshIndicator(
        onRefresh: onRefresh,
        child: SingleChildScrollView(
          physics: const AlwaysScrollableScrollPhysics(),
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: constraints.maxHeight),
            child: Padding(
              padding: EdgeInsets.fromLTRB(14, topPad + 6, 14, bottomPad),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  _Hero(
                    name: name,
                    online: online,
                    totalNodes: data.nodes.length,
                    obs24h: data.recentObs.length,
                    targets: data.targets.length,
                    unread: unread,
                  ),
                  const SizedBox(height: 12),
                  SizedBox(
                    height: (constraints.maxHeight - topPad - bottomPad - 180)
                        .clamp(360.0, 900.0),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Expanded(
                          flex: 30,
                          child: _ActivityPanel(obs: data.recentObs),
                        ),
                        const SizedBox(height: 12),
                        Expanded(
                          flex: 38,
                          child: _TargetsPanel(targets: data.targets),
                        ),
                        const SizedBox(height: 12),
                        Expanded(
                          flex: 28,
                          child: _AlertsPanel(alerts: data.alerts),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

// ── Hero: greeting + stat orbs ────────────────────────────────────────────────

class _Hero extends StatelessWidget {
  const _Hero({
    required this.name,
    required this.online,
    required this.totalNodes,
    required this.obs24h,
    required this.targets,
    required this.unread,
  });

  final String name;
  final int online;
  final int totalNodes;
  final int obs24h;
  final int targets;
  final int unread;

  String get _greeting {
    final h = DateTime.now().hour;
    if (h < 5) return 'Clear skies';
    if (h < 12) return 'Good morning';
    if (h < 18) return 'Good afternoon';
    return 'Good evening';
  }

  @override
  Widget build(BuildContext context) {
    final first = name.trim().split(' ').first;
    final allOnline = totalNodes > 0 && online == totalNodes;
    final networkColor = totalNodes == 0
        ? BSTheme.ink3
        : allOnline
            ? BSTheme.success
            : BSTheme.warm;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    first.isEmpty ? _greeting : '$_greeting, $first',
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 22,
                      fontWeight: FontWeight.w700,
                      letterSpacing: -0.8,
                      color: BSTheme.ink,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                  const SizedBox(height: 2),
                  Text(
                    DateFormat('EEEE • d MMM').format(DateTime.now()),
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 12,
                      color: BSTheme.ink3,
                      letterSpacing: 0.2,
                    ),
                  ),
                ],
              ),
            ),
            // Network status pill
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 7),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(100),
                color: networkColor.withValues(alpha: 0.12),
                border: Border.all(color: networkColor.withValues(alpha: 0.32)),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  _LiveDot(color: networkColor),
                  const SizedBox(width: 7),
                  Text(
                    totalNodes == 0 ? 'No telescopes' : '$online/$totalNodes live',
                    style: TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                      letterSpacing: 0.2,
                      color: networkColor,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
        const SizedBox(height: 14),
        Row(
          children: [
            Expanded(
              child: StatOrb(
                value: online,
                label: 'Online',
                color: BSTheme.success,
                icon: Icons.satellite_alt,
              ),
            ),
            const SizedBox(width: 9),
            Expanded(
              child: StatOrb(
                value: obs24h,
                label: 'Obs 24h',
                color: BSTheme.accent,
                icon: Icons.auto_graph,
              ),
            ),
            const SizedBox(width: 9),
            Expanded(
              child: StatOrb(
                value: targets,
                label: 'Targets',
                color: BSTheme.warm,
                icon: Icons.my_location,
              ),
            ),
            const SizedBox(width: 9),
            Expanded(
              child: StatOrb(
                value: unread,
                label: 'Alerts',
                color: unread > 0 ? BSTheme.danger : BSTheme.ink3,
                icon: Icons.notifications_active,
              ),
            ),
          ],
        ),
      ],
    );
  }
}

// ── Recent activity panel — sparkline + rows ──────────────────────────────────

class _ActivityPanel extends StatelessWidget {
  const _ActivityPanel({required this.obs});
  final List<Observation> obs;

  @override
  Widget build(BuildContext context) {
    final spark = obs.reversed.map((o) => o.magnitude).toList();

    return GlassPanel(
      glow: BSTheme.accent,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const GlassSectionHeader(
            icon: Icons.show_chart,
            label: 'RECENT ACTIVITY',
            detail: 'last 24 h',
          ),
          if (obs.isEmpty)
            const Expanded(child: _EmptyLine('No observations in the last 24 hours.'))
          else ...[
            const SizedBox(height: 10),
            Sparkline(values: spark, color: BSTheme.accent, height: 30),
            const SizedBox(height: 6),
            Expanded(
              child: ClipRect(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children:
                      obs.take(3).map((o) => _ActivityRow(obs: o)).toList(),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _ActivityRow extends StatelessWidget {
  const _ActivityRow({required this.obs});
  final Observation obs;

  @override
  Widget build(BuildContext context) {
    final target = obs.targetName.isEmpty ? '—' : obs.targetName;
    final magColor = obs.magnitude < 8
        ? BSTheme.warm
        : obs.magnitude < 11
            ? BSTheme.accent
            : BSTheme.ink2;

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5.5),
      child: Row(
        children: [
          Icon(Icons.star_rounded, size: 13, color: magColor),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              target,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                fontWeight: FontWeight.w500,
                color: BSTheme.ink,
              ),
              overflow: TextOverflow.ellipsis,
            ),
          ),
          Text(
            obs.magnitude.toStringAsFixed(2),
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 13,
              fontWeight: FontWeight.w700,
              letterSpacing: -0.3,
              color: magColor,
            ),
          ),
          if (obs.filter.isNotEmpty) ...[
            const SizedBox(width: 6),
            GlowChip(obs.filter.toUpperCase()),
          ],
          const SizedBox(width: 8),
          Text(
            _ago(DateTime.tryParse(obs.receivedAt)),
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 11,
              color: BSTheme.ink3,
            ),
          ),
        ],
      ),
    );
  }
}

// ── Network targets panel ─────────────────────────────────────────────────────

class _TargetsPanel extends StatelessWidget {
  const _TargetsPanel({required this.targets});
  final List<Target> targets;

  @override
  Widget build(BuildContext context) {
    final sorted = [...targets]
      ..sort((a, b) => b.priority.compareTo(a.priority));

    return GlassPanel(
      glow: BSTheme.warm,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          GlassSectionHeader(
            icon: Icons.my_location,
            label: 'NETWORK TARGETS',
            detail: '${targets.length} active',
            color: BSTheme.warm,
          ),
          if (targets.isEmpty)
            const Expanded(child: _EmptyLine('No active targets.'))
          else ...[
            const SizedBox(height: 6),
            Expanded(
              child: ClipRect(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: sorted
                      .take(4)
                      .map((t) => _TargetRow(target: t))
                      .toList(),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _TargetRow extends StatelessWidget {
  const _TargetRow({required this.target});
  final Target target;

  @override
  Widget build(BuildContext context) {
    final p = target.priority.clamp(0.0, 1.0);
    final barColor = p > 0.7
        ? BSTheme.accent
        : p > 0.4
            ? BSTheme.warm
            : BSTheme.ink3;
    final typeLabel =
        target.targetType.isEmpty ? '—' : target.targetType.toUpperCase();

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  target.name,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                    color: BSTheme.ink,
                  ),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              const SizedBox(width: 8),
              GlowChip(typeLabel, color: barColor),
              const SizedBox(width: 8),
              Text(
                '${target.nMeasurements}',
                style: TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                  letterSpacing: -0.3,
                  color: barColor,
                ),
              ),
              const SizedBox(width: 3),
              const Text(
                'obs',
                style:
                    TextStyle(fontFamily: 'Geist', fontSize: 10, color: BSTheme.ink3),
              ),
            ],
          ),
          const SizedBox(height: 6),
          // Animated glowing priority track.
          ClipRRect(
            borderRadius: BorderRadius.circular(2),
            child: Stack(
              children: [
                Container(height: 4, color: BSTheme.glassBorder),
                TweenAnimationBuilder<double>(
                  tween: Tween(begin: 0, end: p),
                  duration: const Duration(milliseconds: 900),
                  curve: Curves.easeOutCubic,
                  builder: (_, v, __) => FractionallySizedBox(
                    widthFactor: v,
                    child: Container(
                      height: 4,
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(2),
                        gradient: LinearGradient(
                          colors: [
                            barColor.withValues(alpha: 0.5),
                            barColor,
                          ],
                        ),
                        boxShadow: [
                          BoxShadow(
                            color: barColor.withValues(alpha: 0.6),
                            blurRadius: 6,
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// ── Alerts panel ──────────────────────────────────────────────────────────────

class _AlertsPanel extends StatelessWidget {
  const _AlertsPanel({required this.alerts});
  final List<AppNotification> alerts;

  @override
  Widget build(BuildContext context) {
    final unread = alerts.where((a) => !a.read).length;

    return GlassPanel(
      glow: unread > 0 ? BSTheme.danger : BSTheme.accent,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          GlassSectionHeader(
            icon: Icons.notifications_active,
            label: 'ALERTS',
            detail: unread > 0 ? '$unread unread' : 'all clear',
            color: unread > 0 ? BSTheme.danger : BSTheme.success,
          ),
          if (alerts.isEmpty)
            const Expanded(child: _EmptyLine('All quiet.'))
          else ...[
            const SizedBox(height: 6),
            Expanded(
              child: ClipRect(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children:
                      alerts.take(3).map((a) => _AlertRow(alert: a)).toList(),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _AlertRow extends StatelessWidget {
  const _AlertRow({required this.alert});
  final AppNotification alert;

  @override
  Widget build(BuildContext context) {
    final unread = !alert.read;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5.5),
      child: Row(
        children: [
          Container(
            width: 6,
            height: 6,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: unread ? BSTheme.accent : Colors.transparent,
              border: Border.all(
                color: unread ? BSTheme.accent : BSTheme.ink3,
                width: 1,
              ),
              boxShadow: unread
                  ? [
                      BoxShadow(
                        color: BSTheme.accent.withValues(alpha: 0.5),
                        blurRadius: 4,
                      ),
                    ]
                  : null,
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              alert.title,
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                fontWeight: unread ? FontWeight.w500 : FontWeight.w400,
                color: unread ? BSTheme.ink : BSTheme.ink2,
              ),
              overflow: TextOverflow.ellipsis,
            ),
          ),
          const SizedBox(width: 8),
          Text(
            _ago(DateTime.tryParse(alert.sentAt)),
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 11,
              color: BSTheme.ink3,
            ),
          ),
        ],
      ),
    );
  }
}

// ── Small helpers ─────────────────────────────────────────────────────────────

class _EmptyLine extends StatelessWidget {
  const _EmptyLine(this.text);
  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Text(
        text,
        style: const TextStyle(
          fontFamily: 'Geist',
          fontSize: 13,
          color: BSTheme.ink3,
        ),
      ),
    );
  }
}

class _LiveDot extends StatefulWidget {
  const _LiveDot({required this.color});
  final Color color;

  @override
  State<_LiveDot> createState() => _LiveDotState();
}

class _LiveDotState extends State<_LiveDot> with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1600),
    )..repeat(reverse: true);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _ctrl,
      builder: (_, __) {
        final v = 0.3 + _ctrl.value * 0.7;
        return Container(
          width: 7,
          height: 7,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: widget.color,
            boxShadow: [
              BoxShadow(
                color: widget.color.withValues(alpha: v * 0.9),
                blurRadius: v * 8,
                spreadRadius: v * 1.2,
              ),
            ],
          ),
        );
      },
    );
  }
}

String _ago(DateTime? dt) {
  if (dt == null) return '';
  final diff = DateTime.now().difference(dt.toLocal());
  if (diff.inSeconds < 60) return 'just now';
  if (diff.inMinutes < 60) return '${diff.inMinutes}m ago';
  if (diff.inHours < 24) return '${diff.inHours}h ago';
  return DateFormat.MMMd().format(dt.toLocal());
}
