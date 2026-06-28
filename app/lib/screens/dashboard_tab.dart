import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/aladin_sky.dart';
import '../widgets/glass.dart';
import 'target_detail_screen.dart';

/// "Tonight" — live mission-control dashboard. No scrolling: a hero stat band
/// over the Aladin sky, then an asymmetric two-column glass layout.
class DashboardTab extends StatefulWidget {
  const DashboardTab({super.key, this.onNavigateToTab});

  final void Function(int)? onNavigateToTab;

  @override
  State<DashboardTab> createState() => _DashboardTabState();
}

// ── Data bundle ───────────────────────────────────────────────────────────────

class _DashboardData {
  const _DashboardData({
    required this.nodes,
    required this.recentObs,
    required this.timeline,
    required this.targets,
    required this.alerts,
  });

  final List<Node> nodes;
  final List<Observation> recentObs;
  final List<TimelineItem> timeline;
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

    final nodesFuture = api.nodes().catchError((_) => <Node>[]);
    final obsFuture =
        api.observations(days: 1, limit: 10).catchError((_) => <Observation>[]);
    final timelineFuture = api.timeline().catchError((_) => <TimelineItem>[]);
    final targetsFuture = api.targets().catchError((_) => <Target>[]);
    final notifsFuture = api.notifications(limit: 5);

    List<AppNotification> alerts;
    var unread = 0;
    try {
      final notifs = await notifsFuture;
      alerts = notifs.$1;
      unread = notifs.$2;
    } catch (_) {
      alerts = [];
    }

    if (mounted) {
      context.read<AppState>().setUnreadNotifications(unread);
    }

    return _DashboardData(
      nodes: await nodesFuture,
      recentObs: await obsFuture,
      timeline: await timelineFuture,
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
        return _DashboardView(
          data: snap.data!,
          onRefresh: _refresh,
          onNavigateToTab: widget.onNavigateToTab,
        );
      },
    );
  }
}

// ── Dashboard view — staggered entrance ──────────────────────────────────────

class _DashboardView extends StatefulWidget {
  const _DashboardView({
    required this.data,
    required this.onRefresh,
    this.onNavigateToTab,
  });
  final _DashboardData data;
  final Future<void> Function() onRefresh;
  final void Function(int)? onNavigateToTab;

  @override
  State<_DashboardView> createState() => _DashboardViewState();
}

class _DashboardViewState extends State<_DashboardView> {
  static const _delays = [0, 180, 300, 420];
  final List<bool> _visible = [false, false, false, false];

  @override
  void initState() {
    super.initState();
    for (var i = 0; i < _delays.length; i++) {
      Future.delayed(Duration(milliseconds: _delays[i]), () {
        if (mounted) setState(() => _visible[i] = true);
      });
    }
  }

  Widget _fadeUp(int index, Widget child) {
    return AnimatedOpacity(
      opacity: _visible[index] ? 1.0 : 0.0,
      duration: const Duration(milliseconds: 700),
      curve: Curves.easeOutCubic,
      child: AnimatedSlide(
        offset: _visible[index] ? Offset.zero : const Offset(0, 0.04),
        duration: const Duration(milliseconds: 700),
        curve: Curves.easeOutCubic,
        child: child,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final topPad = MediaQuery.of(context).padding.top + kToolbarHeight;
    final bottomPad = MediaQuery.of(context).padding.bottom + 64;
    final name = context.select<AppState, String>(
      (s) => s.member?.displayName ?? '',
    );

    final online = widget.data.nodes.where((n) => n.online).length;
    final unread = widget.data.alerts.where((a) => !a.read).length;
    final totalNodes = widget.data.nodes.length;
    final needsAction = widget.data.nodes.where(_nodeNeedsAction).toList();
    final priorityTargets = [...widget.data.targets]
      ..sort((a, b) => b.priority.compareTo(a.priority));

    return RefreshIndicator(
      onRefresh: widget.onRefresh,
      child: CustomScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        slivers: [
          SliverPadding(
            padding: EdgeInsets.fromLTRB(16, topPad + 12, 16, bottomPad + 18),
            sliver: SliverList(
              delegate: SliverChildListDelegate([
                _fadeUp(
                  0,
                  _TonightBriefHero(
                    name: name,
                    online: online,
                    totalNodes: totalNodes,
                    obs24h: widget.data.recentObs.length,
                    unread: unread,
                    actionCount: needsAction.length,
                    topTarget: priorityTargets.isEmpty
                        ? null
                        : priorityTargets.first,
                    onAlertsTap: () => widget.onNavigateToTab?.call(3),
                  ),
                ),
                const SizedBox(height: 12),
                LayoutBuilder(
                  builder: (context, constraints) {
                    final wide = constraints.maxWidth >= 760;
                    final readiness = _fadeUp(
                      1,
                      _ReadinessPanel(
                        nodes: widget.data.nodes,
                        alerts: widget.data.alerts,
                        onOpenAlerts: () => widget.onNavigateToTab?.call(3),
                      ),
                    );
                    final plan = _fadeUp(
                      2,
                      _PlanPanel(
                        timeline: widget.data.timeline,
                        targets: priorityTargets,
                      ),
                    );
                    if (!wide) {
                      return Column(
                        children: [
                          readiness,
                          const SizedBox(height: 12),
                          plan,
                        ],
                      );
                    }
                    return Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(flex: 44, child: readiness),
                        const SizedBox(width: 12),
                        Expanded(flex: 56, child: plan),
                      ],
                    );
                  },
                ),
                const SizedBox(height: 12),
                _fadeUp(
                  3,
                  _EvidencePanel(
                    obs: widget.data.recentObs,
                    targets: priorityTargets,
                  ),
                ),
              ]),
            ),
          ),
        ],
      ),
    );
  }
}

bool _nodeNeedsAction(Node node) =>
    !node.online || node.isSleeping || node.isOnVacation;

class _TonightBriefHero extends StatelessWidget {
  const _TonightBriefHero({
    required this.name,
    required this.online,
    required this.totalNodes,
    required this.obs24h,
    required this.unread,
    required this.actionCount,
    required this.topTarget,
    required this.onAlertsTap,
  });

  final String name;
  final int online;
  final int totalNodes;
  final int obs24h;
  final int unread;
  final int actionCount;
  final Target? topTarget;
  final VoidCallback onAlertsTap;

  String get _firstName {
    final trimmed = name.trim();
    return trimmed.isEmpty ? '' : trimmed.split(' ').first;
  }

  String get _headline {
    if (totalNodes == 0) return 'Connect a telescope to start observing.';
    if (actionCount > 0) return 'Tonight needs $actionCount check.';
    if (obs24h > 0) return 'Your network produced data today.';
    if (online > 0) return 'Your telescopes are ready for tonight.';
    return 'Your telescopes need attention.';
  }

  String get _summary {
    final target = topTarget?.name;
    if (actionCount > 0) {
      return 'Resolve the action below before the observing window opens.';
    }
    if (target != null && target.isNotEmpty) {
      return '$target is the highest-priority target in the current queue.';
    }
    return 'Nothing urgent is waiting. Check the plan and recent evidence.';
  }

  @override
  Widget build(BuildContext context) {
    final readinessColor = actionCount > 0
        ? BSTheme.warm
        : totalNodes > 0 && online == totalNodes
            ? BSTheme.success
            : BSTheme.danger;

    return _OpsPanel(
      accent: readinessColor,
      padding: EdgeInsets.zero,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SizedBox(
            height: 238,
            child: _MissionField(
              accent: readinessColor,
              alerts: unread,
              online: online,
              targets: topTarget == null ? 0 : 1,
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(18, 16, 18, 18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const Text(
                            'LIVE SKY CONTROL',
                            style: TextStyle(
                              fontFamily: 'Geist',
                              fontSize: 11,
                              fontWeight: FontWeight.w900,
                              letterSpacing: 2.0,
                              color: BSTheme.accent,
                            ),
                          ),
                          const SizedBox(height: 8),
                          Text(
                            _headline.toUpperCase(),
                            style: const TextStyle(
                              fontFamily: 'Geist',
                              fontSize: 34,
                              fontWeight: FontWeight.w900,
                              letterSpacing: 0,
                              height: 1.02,
                              color: BSTheme.ink,
                            ),
                          ),
                          const SizedBox(height: 10),
                          Text(
                            _summary,
                            style: const TextStyle(
                              fontFamily: 'Geist',
                              fontSize: 14,
                              height: 1.45,
                              color: BSTheme.ink2,
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(width: 12),
                    _StatusPill(
                      label: totalNodes == 0 ? 'SETUP' : '$online/$totalNodes LIVE',
                      color: readinessColor,
                    ),
                  ],
                ),
                const SizedBox(height: 18),
                Row(
                  children: [
                    Expanded(
                      child: _ControlReadout(
                        label: 'OBS',
                        value: '$obs24h',
                        color: BSTheme.sky,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: _ControlReadout(
                        label: 'TARGET',
                        value: topTarget == null ? '0' : 'LIVE',
                        color: BSTheme.warm,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: _ControlReadout(
                        label: 'ALERTS',
                        value: '$unread',
                        color: unread > 0 ? BSTheme.danger : BSTheme.success,
                        onTap: onAlertsTap,
                      ),
                    ),
                  ],
                ),
                if (topTarget != null) ...[
                  const SizedBox(height: 14),
                  _PriorityTrack(target: topTarget!),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _MissionField extends StatelessWidget {
  const _MissionField({
    required this.accent,
    required this.alerts,
    required this.online,
    required this.targets,
  });

  final Color accent;
  final int alerts;
  final int online;
  final int targets;

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: const BorderRadius.vertical(top: Radius.circular(4)),
      child: Stack(
        fit: StackFit.expand,
        children: [
          const AladinSky(),
          DecoratedBox(
            decoration: BoxDecoration(
              gradient: LinearGradient(
                colors: [
                  BSTheme.night.withValues(alpha: 0.18),
                  BSTheme.night.withValues(alpha: 0.58),
                ],
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
              ),
            ),
          ),
          DecoratedBox(
            decoration: BoxDecoration(
              border: Border(
                bottom: BorderSide(color: accent.withValues(alpha: 0.24)),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    const Text(
                      'ALADIN LIVE SKY',
                      style: TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 10,
                        fontWeight: FontWeight.w900,
                        letterSpacing: 1.8,
                        color: BSTheme.ink3,
                      ),
                    ),
                    const Spacer(),
                    _StatusPill(
                      label: alerts > 0 ? '$alerts ALERTS' : 'CLEAR',
                      color: alerts > 0 ? BSTheme.danger : accent,
                    ),
                  ],
                ),
                const Spacer(),
                Row(
                  children: [
                    Expanded(
                      child: _MiniDatum(label: 'online', value: '$online'),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: _MiniDatum(label: 'targets', value: '$targets'),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: _MiniDatum(
                        label: 'state',
                        value: alerts > 0 ? 'review' : 'ready',
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _ControlReadout extends StatelessWidget {
  const _ControlReadout({
    required this.label,
    required this.value,
    required this.color,
    this.onTap,
  });

  final String label;
  final String value;
  final Color color;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final child = Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BSTheme.night.withValues(alpha: 0.58),
        borderRadius: BorderRadius.circular(3),
        border: Border.all(color: color.withValues(alpha: 0.30)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 10,
              fontWeight: FontWeight.w900,
              letterSpacing: 1.0,
              color: BSTheme.ink3,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            value,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 26,
              fontWeight: FontWeight.w900,
              color: color,
            ),
          ),
        ],
      ),
    );
    if (onTap == null) return child;
    return GestureDetector(onTap: onTap, child: child);
  }
}

class _MiniDatum extends StatelessWidget {
  const _MiniDatum({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: BSTheme.night.withValues(alpha: 0.58),
        borderRadius: BorderRadius.circular(3),
        border: Border.all(color: BSTheme.ink.withValues(alpha: 0.10)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 9,
              color: BSTheme.ink3,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            value,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 12,
              fontWeight: FontWeight.w900,
              color: BSTheme.ink2,
            ),
          ),
        ],
      ),
    );
  }
}

class _PriorityTrack extends StatelessWidget {
  const _PriorityTrack({required this.target});

  final Target target;

  @override
  Widget build(BuildContext context) {
    final priority = (target.priority * 100).clamp(0, 100).round();
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
      decoration: BoxDecoration(
        color: BSTheme.night.withValues(alpha: 0.42),
        borderRadius: BorderRadius.circular(3),
        border: Border.all(color: BSTheme.ink.withValues(alpha: 0.12)),
      ),
      child: Row(
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: const BoxDecoration(
              shape: BoxShape.circle,
              color: BSTheme.warm,
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              target.name,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                fontWeight: FontWeight.w900,
                color: BSTheme.ink,
              ),
            ),
          ),
          const SizedBox(width: 8),
          Text(
            '$priority',
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 11,
              fontWeight: FontWeight.w900,
              color: BSTheme.ink3,
            ),
          ),
        ],
      ),
    );
  }
}

class _ReadinessPanel extends StatelessWidget {
  const _ReadinessPanel({
    required this.nodes,
    required this.alerts,
    required this.onOpenAlerts,
  });

  final List<Node> nodes;
  final List<AppNotification> alerts;
  final VoidCallback onOpenAlerts;

  @override
  Widget build(BuildContext context) {
    final unread = alerts.where((a) => !a.read).length;
    final items = <_ReadinessItem>[
      _ReadinessItem(
        title: nodes.isEmpty
            ? 'No telescope connected'
            : '${nodes.where((n) => n.online).length}/${nodes.length} telescopes live',
        detail: nodes.isEmpty
            ? 'Connect a node before it can receive assignments.'
            : 'Offline or sleeping nodes are called out here.',
        color: nodes.isEmpty || nodes.any((n) => !n.online)
            ? BSTheme.warm
            : BSTheme.success,
        icon: Icons.satellite_alt,
      ),
      _ReadinessItem(
        title: unread == 0 ? 'Alerts quiet' : '$unread alert${unread == 1 ? '' : 's'} waiting',
        detail: unread == 0
            ? 'Nothing requires member attention right now.'
            : 'Review incoming notices before tonight continues.',
        color: unread == 0 ? BSTheme.success : BSTheme.danger,
        icon: Icons.notifications_active,
        onTap: unread == 0 ? null : onOpenAlerts,
      ),
    ];

    final actionNodes = nodes.where(_nodeNeedsAction).take(2).map((node) {
      return _ReadinessItem(
        title: node.telescopeModel.isEmpty ? 'Telescope needs review' : node.telescopeModel,
        detail: _nodeActionText(node),
        color: node.online ? BSTheme.warm : BSTheme.danger,
        icon: Icons.build_circle_outlined,
      );
    });

    return _OpsPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const _PanelHeader(
            label: 'READINESS',
            title: 'What needs attention',
            icon: Icons.fact_check_outlined,
          ),
          const SizedBox(height: 12),
          ...items.map((item) => _ReadinessRow(item: item)),
          ...actionNodes.map((item) => _ReadinessRow(item: item)),
        ],
      ),
    );
  }
}

class _PlanPanel extends StatelessWidget {
  const _PlanPanel({required this.timeline, required this.targets});

  final List<TimelineItem> timeline;
  final List<Target> targets;

  @override
  Widget build(BuildContext context) {
    final planItems = timeline.take(4).toList();

    return _OpsPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const _PanelHeader(
            label: 'PLAN',
            title: 'What the network will try',
            icon: Icons.route_outlined,
          ),
          const SizedBox(height: 12),
          if (planItems.isEmpty)
            ...targets.take(3).map((target) => _TargetIntentRow(target: target))
          else
            ...planItems.map((item) => _TimelineIntentRow(item: item)),
          if (planItems.isEmpty && targets.isEmpty)
            const _EmptyLine('No scheduled assignments yet.'),
        ],
      ),
    );
  }
}

class _EvidencePanel extends StatelessWidget {
  const _EvidencePanel({required this.obs, required this.targets});

  final List<Observation> obs;
  final List<Target> targets;

  @override
  Widget build(BuildContext context) {
    final accepted = obs.where((o) => o.aavsoSubmitted).length;
    final values = obs.reversed.map((o) => o.magnitude).toList();

    return _OpsPanel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _PanelHeader(
            label: 'EVIDENCE',
            title: obs.isEmpty ? 'No measurements yet today' : 'Recent measurements',
            detail: obs.isEmpty ? null : '$accepted accepted',
            icon: Icons.science_outlined,
          ),
          const SizedBox(height: 12),
          if (obs.isNotEmpty) ...[
            SizedBox(
              height: 112,
              child: Sparkline(
                values: values,
                color: BSTheme.sky,
                height: 112,
              ),
            ),
            const SizedBox(height: 10),
            ...obs.take(4).map(
                  (o) => _EvidenceRow(
                    obs: o,
                    onTap: () => _openTarget(context, o.targetName),
                  ),
                ),
          ] else if (targets.isNotEmpty)
            ...targets.take(3).map((target) => _TargetIntentRow(target: target))
          else
            const _EmptyLine('Observations will appear here after a clear run.'),
        ],
      ),
    );
  }
}

class _OpsPanel extends StatelessWidget {
  const _OpsPanel({
    required this.child,
    this.padding = const EdgeInsets.all(16),
    this.accent = BSTheme.accent,
  });

  final Widget child;
  final EdgeInsets padding;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    return Stack(
      children: [
        Container(
          padding: padding,
          decoration: BoxDecoration(
            color: BSTheme.surface.withValues(alpha: 0.88),
            borderRadius: BorderRadius.circular(4),
            border: Border.all(color: accent.withValues(alpha: 0.24)),
            boxShadow: const [
              BoxShadow(
                color: Color(0x73000000),
                blurRadius: 22,
                offset: Offset(0, 13),
              ),
            ],
          ),
          child: child,
        ),
        Positioned(
          left: 0,
          top: 0,
          child: _CornerMark(color: accent),
        ),
        Positioned(
          right: 0,
          bottom: 0,
          child: Transform.rotate(
            angle: 3.14159,
            child: _CornerMark(color: accent),
          ),
        ),
      ],
    );
  }
}

class _CornerMark extends StatelessWidget {
  const _CornerMark({required this.color});

  final Color color;

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      child: CustomPaint(
        size: const Size(22, 22),
        painter: _CornerMarkPainter(color: color),
      ),
    );
  }
}

class _CornerMarkPainter extends CustomPainter {
  const _CornerMarkPainter({required this.color});

  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color.withValues(alpha: 0.72)
      ..strokeWidth = 1.6
      ..style = PaintingStyle.stroke;
    canvas.drawLine(Offset.zero, Offset(size.width, 0), paint);
    canvas.drawLine(Offset.zero, Offset(0, size.height), paint);
  }

  @override
  bool shouldRepaint(covariant _CornerMarkPainter oldDelegate) {
    return oldDelegate.color != color;
  }
}

class _PanelHeader extends StatelessWidget {
  const _PanelHeader({
    required this.label,
    required this.title,
    required this.icon,
    this.detail,
  });

  final String label;
  final String title;
  final IconData icon;
  final String? detail;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          width: 32,
          height: 32,
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(3),
            color: BSTheme.ink.withValues(alpha: 0.05),
            border: Border.all(color: BSTheme.glassBorder),
          ),
          child: Icon(icon, size: 17, color: BSTheme.ink2),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                label,
                style: const TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 10,
                  fontWeight: FontWeight.w900,
                  letterSpacing: 1.2,
                  color: BSTheme.ink3,
                ),
              ),
              const SizedBox(height: 2),
              Text(
                title,
                style: const TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 16,
                  fontWeight: FontWeight.w900,
                  letterSpacing: 0,
                  color: BSTheme.ink,
                ),
              ),
            ],
          ),
        ),
        if (detail != null) _StatusPill(label: detail!, color: BSTheme.success),
      ],
    );
  }
}

class _StatusPill extends StatelessWidget {
  const _StatusPill({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.10),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: color.withValues(alpha: 0.32)),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontFamily: 'Geist',
          fontSize: 10,
          fontWeight: FontWeight.w800,
          letterSpacing: 0.8,
          color: color,
        ),
      ),
    );
  }
}

class _BriefMetric extends StatelessWidget {
  const _BriefMetric({
    required this.label,
    required this.value,
    required this.color,
    this.onTap,
  });

  final String label;
  final String value;
  final Color color;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final child = Container(
      constraints: const BoxConstraints(minWidth: 112),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BSTheme.ink.withValues(alpha: 0.035),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: BSTheme.glassBorder),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            label,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 10,
              color: BSTheme.ink3,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            value,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 20,
              fontWeight: FontWeight.w800,
              letterSpacing: 0,
              color: color,
            ),
          ),
        ],
      ),
    );
    if (onTap == null) return child;
    return GestureDetector(onTap: onTap, child: child);
  }
}

class _ReadinessItem {
  const _ReadinessItem({
    required this.title,
    required this.detail,
    required this.color,
    required this.icon,
    this.onTap,
  });

  final String title;
  final String detail;
  final Color color;
  final IconData icon;
  final VoidCallback? onTap;
}

class _ReadinessRow extends StatelessWidget {
  const _ReadinessRow({required this.item});

  final _ReadinessItem item;

  @override
  Widget build(BuildContext context) {
    return _IntentRow(
      icon: item.icon,
      color: item.color,
      title: item.title,
      detail: item.detail,
      trailing: item.onTap == null
          ? null
          : const Icon(Icons.chevron_right, color: BSTheme.ink3, size: 18),
      onTap: item.onTap,
    );
  }
}

class _TimelineIntentRow extends StatelessWidget {
  const _TimelineIntentRow({required this.item});

  final TimelineItem item;

  @override
  Widget build(BuildContext context) {
    final detail = item.reason.isNotEmpty
        ? item.reason
        : '${item.expCount} exposures'
            '${item.filter.isEmpty ? '' : ' · ${item.filter.toUpperCase()}'}';
    return _IntentRow(
      icon: Icons.schedule,
      color: BSTheme.sky,
      title: item.target.isEmpty ? 'Scheduled target' : item.target,
      detail: '${item.startTime} · $detail',
      onTap: () => _openTarget(context, item.target),
    );
  }
}

class _TargetIntentRow extends StatelessWidget {
  const _TargetIntentRow({required this.target});

  final Target target;

  @override
  Widget build(BuildContext context) {
    final priority = (target.priority * 100).clamp(0, 100).round();
    final type = target.targetType.isEmpty ? 'target' : target.targetType;
    return _IntentRow(
      icon: Icons.my_location,
      color: priority > 70 ? BSTheme.warm : BSTheme.sky,
      title: target.name,
      detail: '$type · priority $priority · ${target.nMeasurements} measurements',
      onTap: () => _openTarget(context, target.name),
    );
  }
}

class _EvidenceRow extends StatelessWidget {
  const _EvidenceRow({required this.obs, this.onTap});

  final Observation obs;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final magColor = obs.magnitude < 8
        ? BSTheme.warm
        : obs.magnitude < 11
            ? BSTheme.sky
            : BSTheme.ink2;
    final status = obs.aavsoSubmitted ? 'AAVSO accepted' : obs.qualityFlag;
    return _IntentRow(
      icon: Icons.scatter_plot_outlined,
      color: magColor,
      title: obs.targetName.isEmpty ? 'Unknown target' : obs.targetName,
      detail: '${obs.magnitude.toStringAsFixed(2)} mag · $status',
      trailing: Text(
        _ago(DateTime.tryParse(obs.receivedAt)),
        style: const TextStyle(
          fontFamily: 'Geist',
          fontSize: 11,
          color: BSTheme.ink3,
        ),
      ),
      onTap: onTap,
    );
  }
}

class _IntentRow extends StatelessWidget {
  const _IntentRow({
    required this.icon,
    required this.color,
    required this.title,
    required this.detail,
    this.trailing,
    this.onTap,
  });

  final IconData icon;
  final Color color;
  final String title;
  final String detail;
  final Widget? trailing;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final row = Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.045),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withValues(alpha: 0.18)),
      ),
      child: Row(
        children: [
          Container(
            width: 34,
            height: 34,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(8),
              color: color.withValues(alpha: 0.10),
            ),
            child: Icon(icon, size: 17, color: color),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    fontWeight: FontWeight.w800,
                    letterSpacing: 0,
                    color: BSTheme.ink,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  detail,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 12,
                    height: 1.35,
                    color: BSTheme.ink2,
                  ),
                ),
              ],
            ),
          ),
          if (trailing != null) ...[
            const SizedBox(width: 8),
            trailing!,
          ],
        ],
      ),
    );
    if (onTap == null) return row;
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: onTap,
        child: row,
      ),
    );
  }
}

String _nodeActionText(Node node) {
  if (!node.online) return 'Last heartbeat is stale. Check power and network.';
  if (node.isSleeping && node.portable) {
    return 'Set an observing site before tonight can begin.';
  }
  if (node.isSleeping) return 'Sleeping now. It will wait for assignment.';
  if (node.isOnVacation) return 'Vacation mode is active until ${node.vacationUntil}.';
  return 'Review this telescope before the next observing window.';
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
    this.onAlertsTap,
  });

  final String name;
  final int online;
  final int totalNodes;
  final int obs24h;
  final int targets;
  final int unread;
  final VoidCallback? onAlertsTap;

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
                      letterSpacing: 0,
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
                  LiveDot(color: networkColor),
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
                onTap: onAlertsTap,
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
  const _ActivityPanel({required this.obs, required this.timeline});
  final List<Observation> obs;
  final List<TimelineItem> timeline;

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
          if (obs.isEmpty && timeline.isEmpty)
            const Expanded(child: _EmptyLine('No observations in the last 24 hours.'))
          else if (obs.isEmpty) ...[
            const SizedBox(height: 10),
            Expanded(
              child: ListView(
                padding: EdgeInsets.zero,
                children: timeline.take(6).map((t) => _TimelineRow(item: t)).toList(),
              ),
            ),
          ]
          else ...[
            const SizedBox(height: 10),
            // Full-height sparkline — dominant visual in the left column.
            Expanded(
              child: LayoutBuilder(
                builder: (_, constraints) => Sparkline(
                  values: spark,
                  color: BSTheme.accent,
                  height: constraints.maxHeight,
                ),
              ),
            ),
            const SizedBox(height: 8),
            ...obs
                .take(4)
                .map((o) => _ActivityRow(obs: o, onTap: () => _openTarget(context, o.targetName))),
          ],
        ],
      ),
    );
  }
}

class _TimelineRow extends StatelessWidget {
  const _TimelineRow({required this.item});
  final TimelineItem item;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Row(
        children: [
          const Icon(Icons.schedule, size: 13, color: BSTheme.accent),
          const SizedBox(width: 8),
          SizedBox(
            width: 42,
            child: Text(
              item.startTime,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 11,
                fontWeight: FontWeight.w700,
                color: BSTheme.accent,
              ),
            ),
          ),
          Expanded(
            child: Text(
              item.target,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                fontWeight: FontWeight.w500,
                color: BSTheme.ink,
              ),
              overflow: TextOverflow.ellipsis,
            ),
          ),
          if (item.filter.isNotEmpty) GlowChip(item.filter.toUpperCase()),
        ],
      ),
    );
  }
}

class _ActivityRow extends StatelessWidget {
  const _ActivityRow({required this.obs, this.onTap});
  final Observation obs;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final target = obs.targetName.isEmpty ? '—' : obs.targetName;
    final magColor = obs.magnitude < 8
        ? BSTheme.warm
        : obs.magnitude < 11
            ? BSTheme.accent
            : BSTheme.ink2;

    return _DashboardTapRow(onTap: onTap, child: Row(
        children: [
          Icon(Icons.star_rounded, size: 13, color: magColor),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              target,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
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
              fontSize: 12,
              fontWeight: FontWeight.w700,
              letterSpacing: 0,
              fontFeatures: const [FontFeature.tabularFigures()],
              color: magColor,
            ),
          ),
          if (obs.filter.isNotEmpty) ...[
            const SizedBox(width: 5),
            GlowChip(obs.filter.toUpperCase()),
          ],
          const SizedBox(width: 6),
          Text(
            _ago(DateTime.tryParse(obs.receivedAt)),
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 10,
              color: BSTheme.ink3,
            ),
          ),
        ],
      ));
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
          Row(
            children: [
              Expanded(
                child: GlassSectionHeader(
                  icon: Icons.my_location,
                  label: 'NETWORK TARGETS',
                  detail: '${targets.length} active',
                  color: BSTheme.warm,
                ),
              ),
              const Icon(
                Icons.chevron_right,
                size: 14,
                color: BSTheme.ink3,
              ),
            ],
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
                      .map((t) => _TargetRow(
                            target: t,
                            onTap: () => _openTarget(context, t.name),
                          ))
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
  const _TargetRow({required this.target, this.onTap});
  final Target target;
  final VoidCallback? onTap;

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

    return _DashboardTapRow(
      onTap: onTap,
      padding: const EdgeInsets.symmetric(vertical: 5),
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
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                    color: BSTheme.ink,
                  ),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              const SizedBox(width: 6),
              GlowChip(typeLabel, color: barColor),
              const SizedBox(width: 6),
              Text(
                '${target.nMeasurements}',
                style: TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 12,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 0,
                  fontFeatures: const [FontFeature.tabularFigures()],
                  color: barColor,
                ),
              ),
              const SizedBox(width: 3),
              const Text(
                'obs',
                style: TextStyle(
                    fontFamily: 'Geist', fontSize: 9, color: BSTheme.ink3),
              ),
            ],
          ),
          const SizedBox(height: 5),
          ClipRRect(
            borderRadius: BorderRadius.circular(2),
            child: Stack(
              children: [
                Container(height: 3, color: BSTheme.glassBorder),
                TweenAnimationBuilder<double>(
                  tween: Tween(begin: 0, end: p),
                  duration: const Duration(milliseconds: 900),
                  curve: Curves.easeOutCubic,
                  builder: (_, v, __) => FractionallySizedBox(
                    widthFactor: v,
                    child: Container(
                      height: 3,
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
  const _AlertsPanel({
    required this.alerts,
    this.onOpenAlerts,
    this.centered = false,
  });
  final List<AppNotification> alerts;
  final VoidCallback? onOpenAlerts;
  final bool centered;

  @override
  Widget build(BuildContext context) {
    final unread = alerts.where((a) => !a.read).length;

    return GlassPanel(
      glow: unread > 0 ? BSTheme.danger : BSTheme.accent,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: GlassSectionHeader(
                  icon: Icons.notifications_active,
                  label: 'ALERTS',
                  detail: unread > 0 ? '$unread unread' : 'all clear',
                  color: unread > 0 ? BSTheme.danger : BSTheme.success,
                ),
              ),
              const Icon(
                Icons.chevron_right,
                size: 14,
                color: BSTheme.ink3,
              ),
            ],
          ),
          if (alerts.isEmpty)
            Expanded(
              child: Center(
                child: _EmptyLine(centered ? 'All quiet.' : 'All quiet.'),
              ),
            )
          else ...[
            const SizedBox(height: 6),
            Expanded(
              child: ClipRect(
                child: Column(
                  mainAxisAlignment: centered
                      ? MainAxisAlignment.center
                      : MainAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children:
                      alerts
                          .take(3)
                          .map((a) => _AlertRow(alert: a, onTap: onOpenAlerts))
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

class _AlertRow extends StatelessWidget {
  const _AlertRow({required this.alert, this.onTap});
  final AppNotification alert;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final unread = !alert.read;
    return _DashboardTapRow(onTap: onTap, child: Row(
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
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              alert.title,
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                fontWeight: unread ? FontWeight.w500 : FontWeight.w400,
                color: unread ? BSTheme.ink : BSTheme.ink2,
              ),
              overflow: TextOverflow.ellipsis,
            ),
          ),
          const SizedBox(width: 6),
          Text(
            _ago(DateTime.tryParse(alert.sentAt)),
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 10,
              color: BSTheme.ink3,
            ),
          ),
        ],
      ));
  }
}

// ── Small helpers ─────────────────────────────────────────────────────────────

void _openTarget(BuildContext context, String name) {
  if (name.isEmpty) return;
  Navigator.push(
    context,
    MaterialPageRoute<void>(
      builder: (_) => TargetDetailScreen(targetName: name),
    ),
  );
}

class _DashboardTapRow extends StatelessWidget {
  const _DashboardTapRow({
    required this.child,
    this.onTap,
    this.padding = const EdgeInsets.symmetric(vertical: 4.5),
  });

  final Widget child;
  final VoidCallback? onTap;
  final EdgeInsets padding;

  @override
  Widget build(BuildContext context) {
    if (onTap == null) {
      return Padding(padding: padding, child: child);
    }
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(8),
        child: Padding(padding: padding, child: child),
      ),
    );
  }
}

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

// ── Full targets list screen ──────────────────────────────────────────────────

class _TargetsListScreen extends StatefulWidget {
  const _TargetsListScreen({required this.targets});
  final List<Target> targets;

  @override
  State<_TargetsListScreen> createState() => _TargetsListScreenState();
}

class _TargetsListScreenState extends State<_TargetsListScreen> {
  static const _programs = [
    ('All', ''),
    ('Variable Stars', 'variable_stars'),
    ('Exoplanets', 'exoplanet_transits'),
    ('Transients', 'transient_follow_up'),
  ];

  String _selectedProgram = '';

  static Color _programColor(String program) => switch (program) {
        'exoplanet_transits'  => BSTheme.accent,
        'transient_follow_up' => BSTheme.danger,
        _                     => BSTheme.warm,
      };

  @override
  Widget build(BuildContext context) {
    final sorted = [...widget.targets]
      ..sort((a, b) => b.priority.compareTo(a.priority));
    final filtered = _selectedProgram.isEmpty
        ? sorted
        : sorted.where((t) => t.scienceProgram == _selectedProgram).toList();

    return Scaffold(
      backgroundColor: BSTheme.night,
      appBar: AppBar(
        backgroundColor: BSTheme.night,
        elevation: 0,
        title: const Text(
          'Network Targets',
          style: TextStyle(
            fontFamily: 'Geist',
            fontSize: 18,
            fontWeight: FontWeight.w700,
            letterSpacing: 0,
            color: BSTheme.ink,
          ),
        ),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: BSTheme.ink2),
          onPressed: () => Navigator.of(context).pop(),
        ),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(48),
          child: SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Row(
              children: _programs.map((entry) {
                final (label, program) = entry;
                final selected = _selectedProgram == program;
                final color = program.isEmpty
                    ? BSTheme.ink2
                    : _programColor(program);
                return Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: GestureDetector(
                    onTap: () => setState(() => _selectedProgram = program),
                    child: AnimatedContainer(
                      duration: const Duration(milliseconds: 180),
                      padding: const EdgeInsets.symmetric(
                          horizontal: 12, vertical: 5),
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(100),
                        color: selected
                            ? color.withValues(alpha: 0.18)
                            : Colors.transparent,
                        border: Border.all(
                          color: selected
                              ? color.withValues(alpha: 0.6)
                              : BSTheme.glassBorder,
                        ),
                      ),
                      child: Text(
                        label,
                        style: TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                          color: selected ? color : BSTheme.ink3,
                          letterSpacing: 0.2,
                        ),
                      ),
                    ),
                  ),
                );
              }).toList(),
            ),
          ),
        ),
      ),
      body: filtered.isEmpty
          ? Center(
              child: Text(
                _selectedProgram.isEmpty
                    ? 'No active targets.'
                    : 'No targets in this program yet.',
                style:
                    const TextStyle(fontFamily: 'Geist', color: BSTheme.ink3),
              ),
            )
          : ListView.separated(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
              itemCount: filtered.length,
              separatorBuilder: (_, __) =>
                  const Divider(color: BSTheme.glassBorder, height: 1),
              itemBuilder: (context, i) {
                final t = filtered[i];
                final p = t.priority.clamp(0.0, 1.0);
                final barColor = t.scienceProgram.isNotEmpty
                    ? _programColor(t.scienceProgram)
                    : (p > 0.7
                        ? BSTheme.accent
                        : p > 0.4
                            ? BSTheme.warm
                            : BSTheme.ink3);
                final typeLabel = t.targetType.isEmpty
                    ? '—'
                    : t.targetType.toUpperCase();

                return GestureDetector(
                  onTap: () => Navigator.of(context).push(
                    MaterialPageRoute<void>(
                      builder: (_) =>
                          TargetDetailScreen(targetName: t.name),
                    ),
                  ),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(vertical: 12),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Row(
                          children: [
                            Expanded(
                              child: Text(
                                t.name,
                                style: const TextStyle(
                                  fontFamily: 'Geist',
                                  fontSize: 15,
                                  fontWeight: FontWeight.w600,
                                  color: BSTheme.ink,
                                ),
                              ),
                            ),
                            const SizedBox(width: 8),
                            GlowChip(typeLabel, color: barColor),
                            const SizedBox(width: 8),
                            Text(
                              '${t.nMeasurements} obs',
                              style: TextStyle(
                                fontFamily: 'Geist',
                                fontSize: 13,
                                fontWeight: FontWeight.w600,
                                color: barColor,
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 8),
                        ClipRRect(
                          borderRadius: BorderRadius.circular(2),
                          child: Stack(
                            children: [
                              Container(height: 3, color: BSTheme.glassBorder),
                              FractionallySizedBox(
                                widthFactor: p,
                                child: Container(
                                  height: 3,
                                  decoration: BoxDecoration(
                                    borderRadius: BorderRadius.circular(2),
                                    gradient: LinearGradient(
                                      colors: [
                                        barColor.withValues(alpha: 0.5),
                                        barColor,
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
                  ),
                );
              },
            ),
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
