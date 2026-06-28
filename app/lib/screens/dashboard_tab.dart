import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/aladin_sky.dart';
import '../widgets/glass.dart';
import 'target_detail_screen.dart';

/// "Tonight" — operational observing plan with telescope status, field preview,
/// active target details, and recent observations grouped into one workspace.
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
  bool _myObservationsOnly = true;

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
    final online = widget.data.nodes.where((n) => n.online).length;
    final unread = widget.data.alerts.where((a) => !a.read).length;
    final totalNodes = widget.data.nodes.length;
    final needsAction = widget.data.nodes.where(_nodeNeedsAction).toList();
    final priorityTargets = [...widget.data.targets]
      ..sort((a, b) => b.priority.compareTo(a.priority));
    final selectedPlan =
        widget.data.timeline.isEmpty ? null : widget.data.timeline.first;
    final selectedTarget = _selectedTargetForPlan(
      selectedPlan,
      priorityTargets,
    );

    return RefreshIndicator(
      onRefresh: widget.onRefresh,
      child: CustomScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        slivers: [
          SliverPadding(
            padding: EdgeInsets.fromLTRB(16, topPad + 12, 16, bottomPad + 18),
            sliver: SliverList(
              delegate: SliverChildListDelegate([
                LayoutBuilder(
                  builder: (context, constraints) {
                    final wide = constraints.maxWidth >= 1040;
                    final telescopePanel = _fadeUp(
                      0,
                      _TelescopeOpsPanel(
                        nodes: widget.data.nodes,
                        unread: unread,
                        onOpenAlerts: () => widget.onNavigateToTab?.call(3),
                      ),
                    );
                    final planPanel = _fadeUp(
                      1,
                      _ObservingPlanPanel(
                        timeline: widget.data.timeline,
                        targets: priorityTargets,
                        selectedPlan: selectedPlan,
                      ),
                    );
                    final fieldPreview = _fadeUp(
                      2,
                      _FieldPreviewPanel(
                        plan: selectedPlan,
                        target: selectedTarget,
                      ),
                    );
                    final targetPanel = _fadeUp(
                      2,
                      _SelectedTargetPanel(
                        plan: selectedPlan,
                        target: selectedTarget,
                      ),
                    );
                    final observations = _fadeUp(
                      3,
                      _RecentObservationsPanel(
                        obs: widget.data.recentObs,
                        myObservationsOnly: _myObservationsOnly,
                        onMyObservationsOnlyChanged: (value) {
                          setState(() => _myObservationsOnly = value);
                        },
                      ),
                    );
                    if (!wide) {
                      return Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          telescopePanel,
                          const SizedBox(height: 10),
                          planPanel,
                          const SizedBox(height: 10),
                          fieldPreview,
                          const SizedBox(height: 10),
                          targetPanel,
                          const SizedBox(height: 10),
                          observations,
                        ],
                      );
                    }
                    return Column(
                      children: [
                        Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            SizedBox(width: 260, child: telescopePanel),
                            const SizedBox(width: 10),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.stretch,
                                children: [
                                  planPanel,
                                  const SizedBox(height: 10),
                                  fieldPreview,
                                ],
                              ),
                            ),
                            const SizedBox(width: 10),
                            SizedBox(width: 330, child: targetPanel),
                          ],
                        ),
                        const SizedBox(height: 10),
                        observations,
                      ],
                    );
                  },
                ),
              ]),
            ),
          ),
        ],
      ),
    );
  }
}

Target? _selectedTargetForPlan(TimelineItem? plan, List<Target> targets) {
  if (targets.isEmpty) return null;
  if (plan == null) return targets.first;
  for (final target in targets) {
    if (target.targetId == plan.targetId ||
        target.name.toLowerCase() == plan.target.toLowerCase()) {
      return target;
    }
  }
  return targets.first;
}

class _TelescopeOpsPanel extends StatelessWidget {
  const _TelescopeOpsPanel({
    required this.nodes,
    required this.unread,
    required this.onOpenAlerts,
  });

  final List<Node> nodes;
  final int unread;
  final VoidCallback onOpenAlerts;

  @override
  Widget build(BuildContext context) {
    final node = nodes.isEmpty ? null : nodes.first;
    final online = nodes.where((n) => n.online).length;
    final selectedLabel = node?.nodeId.isNotEmpty == true
        ? node!.nodeId
        : nodes.length > 1
            ? 'All telescopes'
            : 'No telescope';

    return _OpsPanel(
      padding: EdgeInsets.zero,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _WorkbenchHeader(
            title: 'Telescope',
            trailing: '$online/${nodes.length} online',
            color: online == nodes.length && nodes.isNotEmpty
                ? BSTheme.success
                : BSTheme.warm,
          ),
          Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    LiveDot(
                      color: node?.online == true
                          ? BSTheme.success
                          : BSTheme.danger,
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        selectedLabel,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 18,
                          fontWeight: FontWeight.w800,
                          color: BSTheme.ink,
                        ),
                      ),
                    ),
                    Text(
                      node?.online == true ? 'Online' : 'Offline',
                      style: TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 12,
                        fontWeight: FontWeight.w800,
                        color: node?.online == true
                            ? BSTheme.success
                            : BSTheme.danger,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 4),
                Text(
                  node?.location ?? 'Connect a node to begin observing.',
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 12,
                    color: BSTheme.ink3,
                  ),
                ),
                const SizedBox(height: 18),
                _KeyValueLine(
                  label: 'Status',
                  value: _nodeStatus(node),
                  color: node?.online == true ? BSTheme.success : BSTheme.warm,
                ),
                _KeyValueLine(
                  label: 'Mount',
                  value: node?.online == true ? 'Tracking' : 'Waiting',
                  color: node?.online == true ? BSTheme.success : BSTheme.ink3,
                ),
                _KeyValueLine(
                  label: 'Dome',
                  value: node?.online == true ? 'Open' : 'Unknown',
                  color: node?.online == true ? BSTheme.success : BSTheme.ink3,
                ),
                _KeyValueLine(
                  label: 'Camera',
                  value: node?.online == true ? '-20.3 °C' : 'Unknown',
                ),
                const SizedBox(height: 16),
                _SectionLabel('Conditions'),
                const SizedBox(height: 8),
                const _KeyValueLine(
                  label: 'Sky',
                  value: 'Clear',
                  color: BSTheme.success,
                ),
                const _KeyValueLine(label: 'Seeing', value: '2.1"'),
                const _KeyValueLine(label: 'Humidity', value: '34%'),
                const _KeyValueLine(label: 'Moon', value: '12% · 108° away'),
                const SizedBox(height: 16),
                GestureDetector(
                  onTap: onOpenAlerts,
                  child: _AlertSummary(unread: unread),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _ObservingPlanPanel extends StatelessWidget {
  const _ObservingPlanPanel({
    required this.timeline,
    required this.targets,
    required this.selectedPlan,
  });

  final List<TimelineItem> timeline;
  final List<Target> targets;
  final TimelineItem? selectedPlan;

  @override
  Widget build(BuildContext context) {
    final rows = timeline.isNotEmpty ? timeline.take(4).toList() : <TimelineItem>[];
    final targetFallback = targets.take(4).toList();

    return _OpsPanel(
      padding: EdgeInsets.zero,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _WorkbenchHeader(
            title: "Tonight's observing plan",
            subtitle: _tonightRange(),
            trailing: timeline.isNotEmpty
                ? '${timeline.length} targets'
                : '${targetFallback.length} targets',
          ),
          const _PlanHeaderRow(),
          if (rows.isNotEmpty)
            ...rows.map((item) {
              return _PlanTimelineRow(
                item: item,
                selected: selectedPlan == item,
              );
            })
          else if (targetFallback.isNotEmpty)
            ...targetFallback.map((target) {
              return _PlanTargetRow(target: target);
            })
          else
            const Padding(
              padding: EdgeInsets.all(16),
              child: _EmptyLine('No scheduled assignments yet.'),
            ),
        ],
      ),
    );
  }
}

class _FieldPreviewPanel extends StatelessWidget {
  const _FieldPreviewPanel({required this.plan, required this.target});

  final TimelineItem? plan;
  final Target? target;

  @override
  Widget build(BuildContext context) {
    final hasPointing = plan != null && (plan!.ra != 0 || plan!.dec != 0);
    final label = plan?.target ?? target?.name ?? 'Next target';

    return _OpsPanel(
      padding: EdgeInsets.zero,
      child: SizedBox(
        height: 300,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _WorkbenchHeader(
              title: 'Field preview',
              subtitle: hasPointing ? label : 'Waiting for pointing solution',
              trailing: hasPointing ? '12° FoV · DSS2' : null,
            ),
            Expanded(
              child: Stack(
                fit: StackFit.expand,
                children: [
                  AladinSky(
                    ra: hasPointing ? plan!.ra : null,
                    dec: hasPointing ? plan!.dec : null,
                    fov: 12,
                    targetLabel: label,
                    drift: !hasPointing,
                  ),
                  DecoratedBox(
                    decoration: BoxDecoration(
                      gradient: LinearGradient(
                        colors: [
                          BSTheme.night.withValues(alpha: 0.10),
                          BSTheme.night.withValues(alpha: 0.55),
                        ],
                        begin: Alignment.topCenter,
                        end: Alignment.bottomCenter,
                      ),
                    ),
                  ),
                  Center(
                    child: IgnorePointer(
                      child: Container(
                        width: 72,
                        height: 72,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          border: Border.all(
                            color: BSTheme.sky.withValues(alpha: 0.78),
                            width: 1.4,
                          ),
                        ),
                        child: Center(
                          child: Container(
                            width: 6,
                            height: 6,
                            decoration: const BoxDecoration(
                              color: BSTheme.sky,
                              shape: BoxShape.circle,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                  Positioned(
                    left: 16,
                    right: 16,
                    bottom: 14,
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            hasPointing
                                ? 'RA ${_formatRa(plan!.ra)} · Dec ${_formatDec(plan!.dec)}'
                                : 'The preview will lock to the active target when coordinates are available.',
                            style: const TextStyle(
                              fontFamily: 'Geist',
                              fontSize: 12,
                              color: BSTheme.ink2,
                            ),
                          ),
                        ),
                        if (hasPointing)
                          const Text(
                            'target overlay',
                            style: TextStyle(
                              fontFamily: 'Geist',
                              fontSize: 11,
                              color: BSTheme.ink3,
                            ),
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _SelectedTargetPanel extends StatelessWidget {
  const _SelectedTargetPanel({required this.plan, required this.target});

  final TimelineItem? plan;
  final Target? target;

  @override
  Widget build(BuildContext context) {
    final title = plan?.target ?? target?.name ?? 'No target selected';
    final targetType = target?.targetType.isNotEmpty == true
        ? target!.targetType
        : 'Target';

    return _OpsPanel(
      padding: EdgeInsets.zero,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _WorkbenchHeader(title: 'Selected target'),
          Padding(
            padding: const EdgeInsets.all(18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  title,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 24,
                    fontWeight: FontWeight.w900,
                    color: BSTheme.ink,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  _programSummary(target, targetType),
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    color: BSTheme.ink3,
                  ),
                ),
                const SizedBox(height: 18),
                _SectionLabel('Coordinates'),
                const SizedBox(height: 8),
                _KeyValueLine(
                  label: 'RA',
                  value: plan == null ? '—' : _formatRa(plan!.ra),
                ),
                _KeyValueLine(
                  label: 'Dec',
                  value: plan == null ? '—' : _formatDec(plan!.dec),
                ),
                _KeyValueLine(
                  label: 'Magnitude',
                  value: target?.mag == null
                      ? '—'
                      : '${target!.mag!.toStringAsFixed(2)} ${target!.magBand}',
                ),
                const SizedBox(height: 16),
                _SectionLabel('Transit event'),
                const SizedBox(height: 8),
                _KeyValueLine(label: 'Start', value: plan?.startTime ?? '—'),
                _KeyValueLine(
                  label: 'Exposure',
                  value: plan == null
                      ? '—'
                      : '${plan!.expDur.toStringAsFixed(0)} s',
                ),
                _KeyValueLine(
                  label: 'Images',
                  value: plan == null ? '—' : '${plan!.expCount}',
                ),
                _KeyValueLine(
                  label: 'Filter',
                  value: plan?.filter.isNotEmpty == true
                      ? plan!.filter.toUpperCase()
                      : '—',
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _RecentObservationsPanel extends StatelessWidget {
  const _RecentObservationsPanel({
    required this.obs,
    required this.myObservationsOnly,
    required this.onMyObservationsOnlyChanged,
  });

  final List<Observation> obs;
  final bool myObservationsOnly;
  final ValueChanged<bool> onMyObservationsOnlyChanged;

  @override
  Widget build(BuildContext context) {
    return _OpsPanel(
      padding: EdgeInsets.zero,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _WorkbenchHeader(
            title: 'Recent observations',
            subtitle: myObservationsOnly ? 'My observations' : 'All observations',
            trailingWidget: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Checkbox(
                  value: myObservationsOnly,
                  onChanged: (value) =>
                      onMyObservationsOnlyChanged(value ?? true),
                  visualDensity: VisualDensity.compact,
                  side: const BorderSide(color: BSTheme.glassBorder),
                  activeColor: BSTheme.accent,
                ),
                const Text(
                  'My observations only',
                  style: TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 12,
                    color: BSTheme.ink2,
                  ),
                ),
              ],
            ),
          ),
          const _ObservationHeaderRow(),
          if (obs.isEmpty)
            const Padding(
              padding: EdgeInsets.all(16),
              child: _EmptyLine('No measurements yet today.'),
            )
          else
            ...obs.take(6).map((o) => _ObservationTableRow(obs: o)),
        ],
      ),
    );
  }
}

class _WorkbenchHeader extends StatelessWidget {
  const _WorkbenchHeader({
    required this.title,
    this.subtitle,
    this.trailing,
    this.trailingWidget,
    this.color,
  });

  final String title;
  final String? subtitle;
  final String? trailing;
  final Widget? trailingWidget;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 52,
      padding: const EdgeInsets.symmetric(horizontal: 16),
      decoration: const BoxDecoration(
        color: BSTheme.surface2,
        border: Border(bottom: BorderSide(color: BSTheme.glassBorder)),
      ),
      child: Row(
        children: [
          Text(
            title,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 15,
              fontWeight: FontWeight.w900,
              color: BSTheme.ink,
            ),
          ),
          if (subtitle != null) ...[
            const SizedBox(width: 10),
            Text(
              subtitle!,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                color: BSTheme.ink3,
              ),
            ),
          ],
          const Spacer(),
          if (trailingWidget != null)
            trailingWidget!
          else if (trailing != null)
            Text(
              trailing!,
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                fontWeight: FontWeight.w700,
                color: color ?? BSTheme.ink3,
              ),
            ),
        ],
      ),
    );
  }
}

class _PlanHeaderRow extends StatelessWidget {
  const _PlanHeaderRow();

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 34,
      padding: const EdgeInsets.symmetric(horizontal: 20),
      decoration: const BoxDecoration(
        color: BSTheme.night,
        border: Border(bottom: BorderSide(color: BSTheme.glassBorder)),
      ),
      child: const Row(
        children: [
          SizedBox(width: 20),
          Expanded(flex: 42, child: _TableHeaderText('Target')),
          Expanded(flex: 18, child: _TableHeaderText('Type')),
          Expanded(flex: 24, child: _TableHeaderText('Window (UTC)')),
          Expanded(flex: 16, child: _TableHeaderText('Status')),
        ],
      ),
    );
  }
}

class _PlanTimelineRow extends StatelessWidget {
  const _PlanTimelineRow({required this.item, required this.selected});

  final TimelineItem item;
  final bool selected;

  @override
  Widget build(BuildContext context) {
    final duration = item.estimatedMinutes;
    final status = selected ? 'In progress' : 'Pending';
    return Container(
      constraints: BoxConstraints(minHeight: selected ? 96 : 72),
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
      decoration: BoxDecoration(
        color: selected ? BSTheme.sky.withValues(alpha: 0.08) : BSTheme.surface,
        border: const Border(bottom: BorderSide(color: BSTheme.glassBorder)),
      ),
      child: Row(
        children: [
          SizedBox(
            width: 20,
            child: LiveDot(
              color: selected ? BSTheme.sky : BSTheme.ink3,
              size: selected ? 7 : 5,
            ),
          ),
          Expanded(
            flex: 42,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  item.target.isEmpty ? 'Scheduled target' : item.target,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 15,
                    fontWeight: FontWeight.w900,
                    color: BSTheme.ink,
                  ),
                ),
                const SizedBox(height: 3),
                Text(
                  '${item.expCount} images · ${item.expDur.toStringAsFixed(0)}s · ${item.filter.isEmpty ? 'open' : item.filter.toUpperCase()}',
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 12,
                    color: BSTheme.ink3,
                  ),
                ),
                if (selected) ...[
                  const SizedBox(height: 10),
                  ClipRRect(
                    borderRadius: BorderRadius.circular(2),
                    child: LinearProgressIndicator(
                      minHeight: 3,
                      value: 0.28,
                      backgroundColor: BSTheme.glassBorder,
                      valueColor:
                          const AlwaysStoppedAnimation<Color>(BSTheme.sky),
                    ),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    '${duration.toStringAsFixed(0)} min planned · target overlay active',
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 11,
                      color: BSTheme.ink3,
                    ),
                  ),
                ],
              ],
            ),
          ),
          const Expanded(
            flex: 18,
            child: Text(
              'Transit',
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                color: BSTheme.ink2,
              ),
            ),
          ),
          Expanded(
            flex: 24,
            child: Text(
              item.startTime.isEmpty ? '—' : item.startTime,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                fontWeight: FontWeight.w700,
                color: BSTheme.ink,
              ),
            ),
          ),
          Expanded(
            flex: 16,
            child: Text(
              status,
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                fontWeight: FontWeight.w700,
                color: selected ? BSTheme.sky : BSTheme.ink3,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _PlanTargetRow extends StatelessWidget {
  const _PlanTargetRow({required this.target});

  final Target target;

  @override
  Widget build(BuildContext context) {
    final priority = (target.priority * 100).clamp(0, 100).round();
    return Container(
      constraints: const BoxConstraints(minHeight: 72),
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
      decoration: const BoxDecoration(
        color: BSTheme.surface,
        border: Border(bottom: BorderSide(color: BSTheme.glassBorder)),
      ),
      child: Row(
        children: [
          const SizedBox(width: 20, child: LiveDot(color: BSTheme.ink3, size: 5)),
          Expanded(
            flex: 42,
            child: Text(
              target.name,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 15,
                fontWeight: FontWeight.w900,
                color: BSTheme.ink,
              ),
            ),
          ),
          Expanded(
            flex: 18,
            child: Text(
              target.targetType.isEmpty ? 'Target' : target.targetType,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                color: BSTheme.ink2,
              ),
            ),
          ),
          const Expanded(
            flex: 24,
            child: Text(
              'Pending',
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                color: BSTheme.ink3,
              ),
            ),
          ),
          Expanded(
            flex: 16,
            child: Text(
              'P$priority',
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                color: BSTheme.ink3,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _ObservationHeaderRow extends StatelessWidget {
  const _ObservationHeaderRow();

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 34,
      padding: const EdgeInsets.symmetric(horizontal: 20),
      decoration: const BoxDecoration(
        color: BSTheme.night,
        border: Border(bottom: BorderSide(color: BSTheme.glassBorder)),
      ),
      child: const Row(
        children: [
          Expanded(flex: 18, child: _TableHeaderText('Received')),
          Expanded(flex: 28, child: _TableHeaderText('Target')),
          Expanded(flex: 15, child: _TableHeaderText('Filter')),
          Expanded(flex: 16, child: _TableHeaderText('Magnitude')),
          Expanded(flex: 23, child: _TableHeaderText('Result')),
        ],
      ),
    );
  }
}

class _ObservationTableRow extends StatelessWidget {
  const _ObservationTableRow({required this.obs});

  final Observation obs;

  @override
  Widget build(BuildContext context) {
    final result = obs.aavsoSubmitted ? 'Submitted' : 'Measured';
    return Container(
      constraints: const BoxConstraints(minHeight: 58),
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
      decoration: const BoxDecoration(
        color: BSTheme.surface,
        border: Border(bottom: BorderSide(color: BSTheme.glassBorder)),
      ),
      child: Row(
        children: [
          Expanded(flex: 18, child: _TableText(_shortDate(obs.receivedAt))),
          Expanded(
            flex: 28,
            child: _TableText(obs.targetName, strong: true),
          ),
          Expanded(
            flex: 15,
            child: _TableText(obs.filter.isEmpty ? 'CV' : obs.filter),
          ),
          Expanded(
            flex: 16,
            child: _TableText(obs.magnitude.toStringAsFixed(3)),
          ),
          Expanded(
            flex: 23,
            child: Text(
              result,
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                fontWeight: FontWeight.w700,
                color: obs.aavsoSubmitted ? BSTheme.success : BSTheme.warm,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _TableHeaderText extends StatelessWidget {
  const _TableHeaderText(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      overflow: TextOverflow.ellipsis,
      style: const TextStyle(
        fontFamily: 'Geist',
        fontSize: 11,
        fontWeight: FontWeight.w800,
        color: BSTheme.ink3,
      ),
    );
  }
}

class _TableText extends StatelessWidget {
  const _TableText(this.text, {this.strong = false});

  final String text;
  final bool strong;

  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      overflow: TextOverflow.ellipsis,
      style: TextStyle(
        fontFamily: 'Geist',
        fontSize: 13,
        fontWeight: strong ? FontWeight.w800 : FontWeight.w500,
        color: strong ? BSTheme.ink : BSTheme.ink2,
      ),
    );
  }
}

class _KeyValueLine extends StatelessWidget {
  const _KeyValueLine({
    required this.label,
    required this.value,
    this.color,
  });

  final String label;
  final String value;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Row(
        children: [
          Expanded(
            child: Text(
              label,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                color: BSTheme.ink3,
              ),
            ),
          ),
          Text(
            value,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 12,
              fontWeight: FontWeight.w800,
              color: color ?? BSTheme.ink2,
            ),
          ),
        ],
      ),
    );
  }
}

class _AlertSummary extends StatelessWidget {
  const _AlertSummary({required this.unread});

  final int unread;

  @override
  Widget build(BuildContext context) {
    final clear = unread == 0;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: (clear ? BSTheme.success : BSTheme.danger)
            .withValues(alpha: 0.10),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(
          color: (clear ? BSTheme.success : BSTheme.danger)
              .withValues(alpha: 0.24),
        ),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              clear ? 'All systems nominal.' : '$unread alerts need review.',
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                color: clear ? BSTheme.ink3 : BSTheme.ink,
              ),
            ),
          ),
          _StatusPill(
            label: clear ? '0 active' : '$unread active',
            color: clear ? BSTheme.success : BSTheme.danger,
          ),
        ],
      ),
    );
  }
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      style: const TextStyle(
        fontFamily: 'Geist',
        fontSize: 10,
        fontWeight: FontWeight.w800,
        letterSpacing: 1.0,
        color: BSTheme.ink3,
      ),
    );
  }
}

String _nodeStatus(Node? node) {
  if (node == null) return 'Not connected';
  if (!node.online) return 'Offline';
  if (node.isSleeping) return 'Sleeping';
  if (node.isOnVacation) return 'Vacation';
  return 'Observing';
}

String _tonightRange() {
  final now = DateTime.now();
  final tomorrow = now.add(const Duration(days: 1));
  return '${DateFormat.MMMd().format(now)} – ${DateFormat.MMMd().format(tomorrow)}';
}

String _formatRa(double raDeg) {
  final totalSeconds = (raDeg / 15.0 * 3600).round();
  final hours = (totalSeconds ~/ 3600) % 24;
  final minutes = (totalSeconds % 3600) ~/ 60;
  final seconds = totalSeconds % 60;
  return '${hours}h ${minutes.toString().padLeft(2, '0')}m ${seconds.toString().padLeft(2, '0')}s';
}

String _formatDec(double decDeg) {
  final sign = decDeg < 0 ? '-' : '+';
  final abs = decDeg.abs();
  final degrees = abs.floor();
  final totalMinutes = ((abs - degrees) * 60).round();
  final minutes = totalMinutes % 60;
  final carry = totalMinutes ~/ 60;
  return '$sign${(degrees + carry).toString().padLeft(2, '0')}° ${minutes.toString().padLeft(2, '0')}′';
}

String _programSummary(Target? target, String fallbackType) {
  if (target == null) return fallbackType;
  final program = target.scienceProgram.replaceAll('_', ' ');
  if (program.isEmpty) return fallbackType;
  return '$fallbackType · $program';
}

String _shortDate(String value) {
  final parsed = DateTime.tryParse(value);
  if (parsed == null) return value.isEmpty ? '—' : value;
  return DateFormat.MMMd().add_Hm().format(parsed.toLocal());
}

bool _nodeNeedsAction(Node node) =>
    !node.online || node.isSleeping || node.isOnVacation;

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
