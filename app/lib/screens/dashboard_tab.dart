import 'dart:async';
import 'dart:math' as math;

import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../models/node_status.dart';
import '../state/app_state.dart';
import '../theme.dart';
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
  static const _pollInterval = Duration(seconds: 15);

  _DashboardData? _data;
  Object? _error;
  bool _loading = true;
  Timer? _pollTimer;

  @override
  void initState() {
    super.initState();
    _refresh();
    _pollTimer = Timer.periodic(_pollInterval, (_) => _refresh(silent: true));
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    super.dispose();
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

  // `silent` refreshes (periodic polling) update data in place without
  // dropping back to a loading spinner, so the view doesn't flicker every
  // 15s. Only the initial load and manual pull-to-refresh show the spinner.
  Future<void> _refresh({bool silent = false}) async {
    if (!silent) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final data = await _load();
      if (!mounted) return;
      setState(() {
        _data = data;
        _error = null;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e;
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_data == null) {
      if (_loading) {
        return const Center(child: CircularProgressIndicator());
      }
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.cloud_off, size: 48, color: BSTheme.ink3),
              const SizedBox(height: 12),
              Text(
                '$_error',
                textAlign: TextAlign.center,
                style: const TextStyle(color: BSTheme.ink2),
              ),
              const SizedBox(height: 20),
              ElevatedButton(
                onPressed: () => _refresh(),
                child: const Text('Retry'),
              ),
            ],
          ),
        ),
      );
    }
    return _DashboardView(
      data: _data!,
      onRefresh: () => _refresh(),
      onNavigateToTab: widget.onNavigateToTab,
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
  String? _selectedTargetName;

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
    final unread = widget.data.alerts.where((a) => !a.read).length;
    final priorityTargets = [...widget.data.targets]
      ..sort((a, b) => b.priority.compareTo(a.priority));
    final hasPlan = widget.data.timeline.isNotEmpty;
    TimelineItem? selectedPlan = hasPlan
        ? _selectedPlan(widget.data.timeline, _selectedTargetName)
        : null;
    // Also match against live schedule reports from nodes (scheduleTarget).
    // This ensures "In progress" reflects reality even if plan state is not yet set.
    // Only auto-apply live match when user has not explicitly chosen a target by tapping.
    final noUserSelection =
        _selectedTargetName == null || _selectedTargetName!.isEmpty;
    if (noUserSelection &&
        (selectedPlan == null ||
            selectedPlan.state.toLowerCase() != 'observing') &&
        widget.data.nodes.isNotEmpty &&
        hasPlan) {
      for (final node in widget.data.nodes) {
        final liveTarget = node.conditions.scheduleTarget;
        if (liveTarget.isEmpty) continue;
        for (final item in widget.data.timeline) {
          if (item.target.toLowerCase() == liveTarget.toLowerCase() ||
              item.targetId.toLowerCase() == liveTarget.toLowerCase()) {
            selectedPlan = item;
            break;
          }
        }
        if (selectedPlan != null) break;
      }
    }
    final selectedTarget = _selectedTargetForPlan(
      selectedPlan,
      priorityTargets,
      selectedName: _selectedTargetName,
      hasPlan: hasPlan,
    );

    return LayoutBuilder(
      builder: (context, constraints) {
        final wide = constraints.maxWidth >= 1040;
        final statusBanner = _fadeUp(
          0,
          _LiveStatusBanner(
            nodes: widget.data.nodes,
            planCount: widget.data.timeline.length,
            activePlanTarget: selectedPlan?.target,
          ),
        );
        final telescopePanel = _fadeUp(
          0,
          _TelescopeOpsPanel(
            nodes: widget.data.nodes,
            unread: unread,
            onOpenAlerts: () => widget.onNavigateToTab?.call(99),
          ),
        );
        final planPanel = _fadeUp(
          1,
          _ObservingPlanPanel(
            timeline: widget.data.timeline,
            targets: priorityTargets,
            selectedPlan: selectedPlan,
            selectedTarget: selectedTarget,
            onSelectTarget: (name) =>
                setState(() => _selectedTargetName = name),
          ),
        );
        final targetPanel = _fadeUp(
          2,
          _SelectedTargetPanel(
            plan: selectedPlan,
            target: selectedTarget,
            targets: priorityTargets,
            selectedName: _selectedTargetName,
            onSelectTarget: (name) =>
                setState(() => _selectedTargetName = name),
          ),
        );
        final observations = _fadeUp(
          3,
          _RecentObservationsPanel(
            obs: widget.data.recentObs,
            maxRows: wide ? 3 : 6,
            myObservationsOnly: _myObservationsOnly,
            onMyObservationsOnlyChanged: (value) {
              setState(() => _myObservationsOnly = value);
            },
          ),
        );

        if (!wide) {
          final bottomPad = MediaQuery.of(context).padding.bottom + 64;
          return RefreshIndicator(
            onRefresh: widget.onRefresh,
            child: CustomScrollView(
              physics: const AlwaysScrollableScrollPhysics(),
              slivers: [
                SliverPadding(
                  padding: EdgeInsets.fromLTRB(
                    14,
                    topPad,
                    14,
                    bottomPad + 14,
                  ),
                  sliver: SliverList(
                    delegate: SliverChildListDelegate([
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          statusBanner,
                          const SizedBox(height: 10),
                          telescopePanel,
                          const SizedBox(height: 10),
                          planPanel,
                          const SizedBox(height: 10),
                          targetPanel,
                          const SizedBox(height: 10),
                          observations,
                        ],
                      ),
                    ]),
                  ),
                ),
              ],
            ),
          );
        }

        const desktopTopPad = kToolbarHeight;
        final availableHeight = constraints.maxHeight - desktopTopPad - 18;
        final observationHeight = (availableHeight * 0.22).clamp(142.0, 190.0);

        return Padding(
          padding: const EdgeInsets.fromLTRB(12, desktopTopPad, 12, 12),
          child: Column(
            children: [
              statusBanner,
              const SizedBox(height: 10),
              Expanded(
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    SizedBox(width: 258, child: telescopePanel),
                    const SizedBox(width: 10),
                    Expanded(child: planPanel),
                    const SizedBox(width: 10),
                    SizedBox(width: 326, child: targetPanel),
                  ],
                ),
              ),
              const SizedBox(height: 10),
              SizedBox(height: observationHeight, child: observations),
            ],
          ),
        );
      },
    );
  }
}

TimelineItem? _selectedPlan(List<TimelineItem> timeline, String? selectedName) {
  if (timeline.isEmpty) return null;
  if (selectedName != null && selectedName.isNotEmpty) {
    for (final item in timeline) {
      if (item.target.toLowerCase() == selectedName.toLowerCase() ||
          item.targetId.toLowerCase() == selectedName.toLowerCase()) {
        return item;
      }
    }
    // fallthrough to auto-pick below if explicit name not present
  }
  // Prefer a server-reported in-progress item; do not blindly treat the first
  // (often earliest or already complete) plan item as "in progress".
  for (final item in timeline) {
    if (item.state.toLowerCase() == 'observing') {
      return item;
    }
  }
  return null;
}

Target? _selectedTargetForPlan(
  TimelineItem? plan,
  List<Target> targets, {
  String? selectedName,
  bool hasPlan = false,
}) {
  if (selectedName != null && selectedName.isNotEmpty) {
    for (final target in targets) {
      if (target.name.toLowerCase() == selectedName.toLowerCase() ||
          target.targetId.toLowerCase() == selectedName.toLowerCase()) {
        return target;
      }
    }
  }
  if (plan != null) {
    for (final target in targets) {
      if (target.targetId == plan.targetId ||
          target.name.toLowerCase() == plan.target.toLowerCase()) {
        return target;
      }
    }
    return null;
  }
  return null;
}

class _LiveStatusBanner extends StatelessWidget {
  const _LiveStatusBanner({
    required this.nodes,
    required this.planCount,
    this.activePlanTarget,
  });

  final List<Node> nodes;
  final int planCount;
  final String? activePlanTarget;

  @override
  Widget build(BuildContext context) {
    final node = nodes.isEmpty ? null : nodes.first;
    final status = primaryNodeStatus(
      node: node,
      planCount: planCount,
      activePlanTarget: activePlanTarget,
    );

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: status.color.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: status.color.withValues(alpha: 0.28)),
        boxShadow: [
          BoxShadow(
            color: status.color.withValues(alpha: 0.12),
            blurRadius: 18,
            offset: const Offset(0, 8),
          ),
        ],
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(8),
              color: status.color.withValues(alpha: 0.14),
            ),
            child: Icon(status.icon, color: status.color, size: 24),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  status.headline,
                  style: TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 20,
                    fontWeight: FontWeight.w900,
                    color: status.color,
                    height: 1.15,
                  ),
                ),
                const SizedBox(height: 6),
                Text(
                  status.detail,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    height: 1.45,
                    color: BSTheme.ink2,
                  ),
                ),
                if (node != null && node.lastHeartbeat.isNotEmpty) ...[
                  const SizedBox(height: 8),
                  Text(
                    'Last heartbeat ${heartbeatAgeLabel(node.lastHeartbeat)}',
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
        ],
      ),
    );
  }
}

class _TelescopeOpsPanel extends StatefulWidget {
  const _TelescopeOpsPanel({
    required this.nodes,
    required this.unread,
    required this.onOpenAlerts,
  });

  final List<Node> nodes;
  final int unread;
  final VoidCallback onOpenAlerts;

  @override
  State<_TelescopeOpsPanel> createState() => _TelescopeOpsPanelState();
}

class _TelescopeOpsPanelState extends State<_TelescopeOpsPanel> {
  List<TelescopeSpec> _catalog = const [];

  @override
  void initState() {
    super.initState();
    _loadCatalog();
  }

  Future<void> _loadCatalog() async {
    try {
      final list = await context.read<AppState>().api.telescopes();
      if (mounted) setState(() => _catalog = list);
    } catch (_) {}
  }

  TelescopeSpec? _specFor(Node? node) {
    if (node == null || node.telescopeModel.isEmpty) return null;
    final model = node.telescopeModel.toLowerCase();
    for (final spec in _catalog) {
      if (spec.isCustom) continue;
      if (spec.displayName.toLowerCase() == model ||
          spec.key.replaceAll('_', ' ') == model) {
        return spec;
      }
    }
    for (final spec in _catalog) {
      if (spec.isCustom) continue;
      if (model.contains(spec.displayName.toLowerCase()) ||
          spec.displayName.toLowerCase().contains(model)) {
        return spec;
      }
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final nodes = widget.nodes;
    final unread = widget.unread;
    final onOpenAlerts = widget.onOpenAlerts;
    final node = nodes.isEmpty ? null : nodes.first;
    final online = nodes.where((n) => n.online).length;
    final selectedLabel = node != null
        ? node.label
        : nodes.length > 1
            ? 'All telescopes'
            : 'No telescope';

    return _OpsPanel(
      padding: EdgeInsets.zero,
      child: _PanelScrollBody(
        header: _WorkbenchHeader(
          title: 'Telescope',
          trailing: '$online/${nodes.length} online',
          color: BSTheme.ink3,
        ),
        bodyPadding: const EdgeInsets.all(12),
        body: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                LiveDot(
                  color: node?.online == true ? BSTheme.accent : BSTheme.ink3,
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
                    color: BSTheme.ink2,
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
              color: node?.online == true ? BSTheme.ink : BSTheme.ink3,
            ),
            if (node?.telescopeModel.isNotEmpty == true)
              _KeyValueLine(label: 'Model', value: node!.telescopeModel),
            if (_specFor(node) case final spec?) ...[
              _KeyValueLine(
                label: 'Aperture',
                value: '${spec.apertureMm.toStringAsFixed(0)} mm',
              ),
              _KeyValueLine(
                label: 'Focal / ƒ',
                value:
                    '${spec.focalLengthMm.toStringAsFixed(0)} mm · ƒ/${spec.focalRatio.toStringAsFixed(1)}',
              ),
              _KeyValueLine(
                label: 'Pixel scale',
                value: '${spec.pixelScaleArcsec.toStringAsFixed(2)}″/px',
              ),
              if (spec.cameraModel.isNotEmpty)
                _KeyValueLine(label: 'Sensor', value: spec.cameraModel),
            ],
            const SizedBox(height: 16),
            GestureDetector(
              onTap: onOpenAlerts,
              child: _AlertSummary(unread: unread),
            ),
          ],
        ),
      ),
    );
  }
}

class _ObservingPlanPanel extends StatelessWidget {
  const _ObservingPlanPanel({
    required this.timeline,
    required this.targets,
    required this.selectedPlan,
    required this.selectedTarget,
    required this.onSelectTarget,
  });

  final List<TimelineItem> timeline;
  final List<Target> targets;
  final TimelineItem? selectedPlan;
  final Target? selectedTarget;
  final ValueChanged<String> onSelectTarget;

  @override
  Widget build(BuildContext context) {
    final rows =
        timeline.isNotEmpty ? timeline.take(4).toList() : <TimelineItem>[];

    return _OpsPanel(
      padding: EdgeInsets.zero,
      child: _PanelScrollBody(
        header: _WorkbenchHeader(
          title: "Tonight's observing plan",
          subtitle: _tonightRange(),
          trailing: timeline.isNotEmpty ? '${timeline.length} targets' : null,
        ),
        bodyPadding: EdgeInsets.zero,
        body: rows.isNotEmpty
            ? Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const _PlanHeaderRow(),
                  ...rows.map((item) {
                    return _PlanTimelineRow(
                      item: item,
                      selected: selectedPlan == item,
                      onTap: () => onSelectTarget(item.target),
                    );
                  }),
                ],
              )
            : const Padding(
                padding: EdgeInsets.all(16),
                child: _EmptyLine(
                  'No observing plan yet. Your telescope will receive assignments when tonight\'s plan is ready.',
                ),
              ),
      ),
    );
  }
}

class _SelectedTargetPanel extends StatefulWidget {
  const _SelectedTargetPanel({
    required this.plan,
    required this.target,
    required this.targets,
    required this.selectedName,
    required this.onSelectTarget,
  });

  final TimelineItem? plan;
  final Target? target;
  final List<Target> targets;
  final String? selectedName;
  final ValueChanged<String> onSelectTarget;

  @override
  State<_SelectedTargetPanel> createState() => _SelectedTargetPanelState();
}

class _SelectedTargetPanelState extends State<_SelectedTargetPanel> {
  late Future<ObjectDetails?> _detailsFuture;
  String? _loadedName;

  @override
  void initState() {
    super.initState();
    _detailsFuture = _loadDetails(_lookupName);
  }

  @override
  void didUpdateWidget(_SelectedTargetPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    final name = _lookupName;
    if (name != _loadedName) {
      setState(() => _detailsFuture = _loadDetails(name));
    }
  }

  String get _lookupName {
    if (widget.selectedName != null && widget.selectedName!.isNotEmpty) {
      return widget.selectedName!;
    }
    if (widget.plan != null && widget.plan!.target.isNotEmpty) {
      return widget.plan!.target;
    }
    return widget.target?.name ?? '';
  }

  String get _title => _lookupName;

  Future<ObjectDetails?> _loadDetails(String name) async {
    _loadedName = name;
    if (name.isEmpty) return null;
    final api = context.read<AppState>().api;
    try {
      return await api.objectDetails(name);
    } catch (_) {
      final id = widget.target?.targetId ?? '';
      if (id.isNotEmpty && id.toLowerCase() != name.toLowerCase()) {
        try {
          return await api.objectDetails(id);
        } catch (_) {}
      }
      return null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final title = _title.isEmpty ? 'No target selected' : _title;
    final hasSelection = _title.isNotEmpty;
    final targetType = widget.target?.targetType.isNotEmpty == true
        ? widget.target!.targetType
        : 'Target';

    return _OpsPanel(
      padding: EdgeInsets.zero,
      child: _PanelScrollBody(
        header: _WorkbenchHeader(
          title: 'Selected target',
          trailingWidget: _TargetPickerButton(
            targets: widget.targets,
            selectedName: title,
            onSelectTarget: widget.onSelectTarget,
          ),
        ),
        body: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              title,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 21,
                fontWeight: FontWeight.w900,
                color: BSTheme.ink,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              _programSummary(widget.target, targetType),
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                color: BSTheme.ink3,
              ),
            ),
            if (!hasSelection) ...[
              const SizedBox(height: 14),
              const _EmptyLine(
                'Select a target from tonight\'s plan to see coordinates and catalogue data.',
              ),
            ] else ...[
              const SizedBox(height: 14),
              FutureBuilder<ObjectDetails?>(
                key: ValueKey('finder-$_lookupName'),
                future: _detailsFuture,
                builder: (context, snap) {
                  final raDeg = (widget.plan != null &&
                          (widget.plan!.ra != 0 || widget.plan!.dec != 0))
                      ? widget.plan!.ra
                      : snap.data?.raDeg;
                  final decDeg = (widget.plan != null &&
                          (widget.plan!.ra != 0 || widget.plan!.dec != 0))
                      ? widget.plan!.dec
                      : snap.data?.decDeg;
                  if (raDeg == null || decDeg == null) {
                    return const SizedBox.shrink();
                  }
                  return Padding(
                    padding: const EdgeInsets.only(bottom: 14),
                    child: _FinderChart(
                      raDeg: raDeg,
                      decDeg: decDeg,
                      label: title,
                    ),
                  );
                },
              ),
              _LightCurveMini(targetName: title),
              const SizedBox(height: 14),
              _SectionLabel('Coordinates'),
              const SizedBox(height: 8),
              _KeyValueLine(
                label: 'RA',
                value: widget.plan == null
                    ? '—'
                    : (widget.plan!.ra != 0 || widget.plan!.dec != 0)
                        ? _formatRa(widget.plan!.ra)
                        : '—',
              ),
              _KeyValueLine(
                label: 'Dec',
                value: widget.plan == null
                    ? '—'
                    : (widget.plan!.ra != 0 || widget.plan!.dec != 0)
                        ? _formatDec(widget.plan!.dec)
                        : '—',
              ),
              _KeyValueLine(
                label: 'Magnitude',
                value: widget.target?.mag == null
                    ? '—'
                    : '${widget.target!.mag!.toStringAsFixed(2)} ${widget.target!.magBand}',
              ),
              if (widget.plan != null) ...[
                const SizedBox(height: 12),
                _SectionLabel('Scheduled observation'),
                const SizedBox(height: 8),
                _KeyValueLine(
                  label: 'Start',
                  value: widget.plan?.startTime ?? '—',
                ),
                _KeyValueLine(
                  label: 'Exposure',
                  value: '${widget.plan!.expDur.toStringAsFixed(0)} s',
                ),
                _KeyValueLine(
                  label: 'Images',
                  value: '${widget.plan!.expCount}',
                ),
                _KeyValueLine(
                  label: 'Filter',
                  value: widget.plan?.filter.isNotEmpty == true
                      ? widget.plan!.filter.toUpperCase()
                      : '—',
                ),
              ],
              const SizedBox(height: 12),
              FutureBuilder<ObjectDetails?>(
                key: ValueKey(_lookupName),
                future: _detailsFuture,
                builder: (context, snap) {
                  if (snap.connectionState == ConnectionState.waiting) {
                    return const _CatalogueLoading();
                  }
                  if (snap.hasError) {
                    return const _EmptyLine(
                      'Could not load catalogue data for this target.',
                    );
                  }
                  final details = snap.data;
                  if (details == null) {
                    return const _EmptyLine(
                      'No catalogue entry found for this target.',
                    );
                  }
                  return _CatalogueDetails(details: details);
                },
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// Live sky view of where this target sits — a real DSS2 cutout centered on
/// its coordinates (via the CDS hips2fits service), with an orange reticle
/// marking the pointing. This is the closest thing to an Aladin finder chart
/// that works without an embedded web view.
class _FinderChart extends StatelessWidget {
  const _FinderChart({
    required this.raDeg,
    required this.decDeg,
    required this.label,
    this.fovDeg = 0.8,
  });

  final double raDeg;
  final double decDeg;
  final String label;
  final double fovDeg;

  String get _imageUrl {
    final params = {
      'hips': 'CDS/P/DSS2/color',
      'width': '440',
      'height': '300',
      'fov': fovDeg.toString(),
      'projection': 'TAN',
      'coordsys': 'icrs',
      'ra': raDeg.toString(),
      'dec': decDeg.toString(),
      'format': 'jpg',
    };
    return Uri.https(
      'alasky.u-strasbg.fr',
      '/hips-image-services/hips2fits',
      params,
    ).toString();
  }

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(4),
      child: AspectRatio(
        aspectRatio: 440 / 300,
        child: Stack(
          fit: StackFit.expand,
          children: [
            Container(color: Colors.black),
            Image.network(
              _imageUrl,
              key: ValueKey(_imageUrl),
              fit: BoxFit.cover,
              loadingBuilder: (context, child, progress) {
                if (progress == null) return child;
                return const Center(
                  child: SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: BSTheme.ink3,
                    ),
                  ),
                );
              },
              errorBuilder: (context, error, stack) => const Center(
                child: Text(
                  'Sky image unavailable',
                  style: TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 11,
                    color: BSTheme.ink3,
                  ),
                ),
              ),
            ),
            IgnorePointer(
              child: Center(
                child: Container(
                  width: 34,
                  height: 34,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: BSTheme.warm.withValues(alpha: 0.9),
                      width: 1.4,
                    ),
                  ),
                ),
              ),
            ),
            Positioned(
              left: 8,
              top: 6,
              child: Text(
                'FINDER CHART — ${label.toUpperCase()}',
                style: const TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 9,
                  fontWeight: FontWeight.w800,
                  letterSpacing: 0.6,
                  color: Colors.white70,
                ),
              ),
            ),
            Positioned(
              right: 8,
              top: 6,
              child: Text(
                '${fovDeg.toStringAsFixed(1)}° FOV · DSS2',
                style: const TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 9,
                  color: Colors.white54,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Compact real photometric light curve for the selected target, pulled from
/// the same /lightcurves endpoint the full target-detail screen uses.
class _LightCurveMini extends StatefulWidget {
  const _LightCurveMini({required this.targetName});

  final String targetName;

  @override
  State<_LightCurveMini> createState() => _LightCurveMiniState();
}

class _LightCurveMiniState extends State<_LightCurveMini> {
  late Future<List<LightCurvePoint>> _future;
  String? _loadedName;

  @override
  void initState() {
    super.initState();
    _future = _load(widget.targetName);
  }

  @override
  void didUpdateWidget(_LightCurveMini oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.targetName != _loadedName) {
      setState(() => _future = _load(widget.targetName));
    }
  }

  Future<List<LightCurvePoint>> _load(String name) async {
    _loadedName = name;
    if (name.isEmpty) return const [];
    try {
      return await context.read<AppState>().api.lightCurve(name, days: 14);
    } catch (_) {
      return const [];
    }
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<List<LightCurvePoint>>(
      key: ValueKey('lc-${widget.targetName}'),
      future: _future,
      builder: (context, snap) {
        if (snap.connectionState == ConnectionState.waiting) {
          return const Padding(
            padding: EdgeInsets.only(bottom: 14),
            child: _CatalogueLoading(),
          );
        }
        final points = snap.data ?? const [];
        return Padding(
          padding: const EdgeInsets.only(bottom: 4),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  const Expanded(child: _SectionLabel('Δ MAG — TIME SERIES')),
                  if (points.isNotEmpty)
                    Text(
                      '${points.length} pts',
                      style: const TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 10,
                        color: BSTheme.ink3,
                      ),
                    ),
                ],
              ),
              const SizedBox(height: 8),
              if (points.length < 2)
                const _EmptyLine('Awaiting photometry.')
              else
                SizedBox(height: 78, child: _LightCurveSpark(points: points)),
            ],
          ),
        );
      },
    );
  }
}

class _LightCurveSpark extends StatelessWidget {
  const _LightCurveSpark({required this.points});

  final List<LightCurvePoint> points;

  @override
  Widget build(BuildContext context) {
    final sorted = [...points]..sort((a, b) => a.bjd.compareTo(b.bjd));
    final origin = sorted.first.bjd;
    final spots =
        sorted.map((p) => FlSpot(p.bjd - origin, -p.magnitude)).toList();
    final mags = sorted.map((p) => p.magnitude).toList();
    final minMag = mags.reduce(math.min);
    final maxMag = mags.reduce(math.max);
    final pad = (maxMag - minMag) * 0.18 + 0.05;

    return LineChart(
      LineChartData(
        minY: -maxMag - pad,
        maxY: -minMag + pad,
        gridData: const FlGridData(show: false),
        borderData: FlBorderData(show: false),
        titlesData: const FlTitlesData(show: false),
        lineTouchData: const LineTouchData(enabled: false),
        lineBarsData: [
          LineChartBarData(
            spots: spots,
            isCurved: true,
            curveSmoothness: 0.25,
            color: BSTheme.warm,
            barWidth: 1.4,
            dotData: FlDotData(
              show: true,
              getDotPainter: (spot, pct, bar, idx) => FlDotCirclePainter(
                radius: 1.8,
                color: BSTheme.warm,
                strokeColor: Colors.transparent,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _TargetPickerButton extends StatelessWidget {
  const _TargetPickerButton({
    required this.targets,
    required this.selectedName,
    required this.onSelectTarget,
  });

  final List<Target> targets;
  final String selectedName;
  final ValueChanged<String> onSelectTarget;

  @override
  Widget build(BuildContext context) {
    return PopupMenuButton<String>(
      tooltip: 'Select target',
      icon: const Icon(Icons.track_changes, color: BSTheme.ink2, size: 20),
      color: BSTheme.surface2,
      onSelected: onSelectTarget,
      itemBuilder: (context) => targets.take(40).map((target) {
        return PopupMenuItem<String>(
          value: target.name,
          child: Text(
            target.name,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              fontFamily: 'Geist',
              color: target.name == selectedName ? BSTheme.sky : BSTheme.ink,
              fontWeight: target.name == selectedName
                  ? FontWeight.w900
                  : FontWeight.w700,
            ),
          ),
        );
      }).toList(),
    );
  }
}

class _CatalogueLoading extends StatelessWidget {
  const _CatalogueLoading();

  @override
  Widget build(BuildContext context) {
    return const Padding(
      padding: EdgeInsets.only(top: 4),
      child: LinearProgressIndicator(
        minHeight: 2,
        backgroundColor: BSTheme.glassBorder,
        valueColor: AlwaysStoppedAnimation<Color>(BSTheme.sky),
      ),
    );
  }
}

class _CatalogueDetails extends StatelessWidget {
  const _CatalogueDetails({required this.details});

  final ObjectDetails details;

  @override
  Widget build(BuildContext context) {
    final rows = <Widget>[
      if (details.canonicalName.isNotEmpty)
        _KeyValueLine(label: 'Catalog name', value: details.canonicalName),
      if (details.objectType.isNotEmpty)
        _KeyValueLine(label: 'Object type', value: details.objectType),
      if (details.spectralType.isNotEmpty)
        _KeyValueLine(label: 'Spectrum', value: details.spectralType),
      if (details.raDeg != null)
        _KeyValueLine(label: 'RA catalog', value: _formatRa(details.raDeg!)),
      if (details.decDeg != null)
        _KeyValueLine(label: 'Dec catalog', value: _formatDec(details.decDeg!)),
      if (details.hostName.isNotEmpty)
        _KeyValueLine(label: 'Host star', value: details.hostName),
      if (details.periodDays != null)
        _KeyValueLine(
          label: 'Period',
          value: '${details.periodDays!.toStringAsFixed(5)} d',
        ),
      if (details.transitDurationHours != null)
        _KeyValueLine(
          label: 'Transit duration',
          value: '${details.transitDurationHours!.toStringAsFixed(2)} h',
        ),
      if (details.transitDepthPpm != null)
        _KeyValueLine(
          label: 'Transit depth',
          value: '${details.transitDepthPpm!.toStringAsFixed(0)} ppm',
        ),
      if (details.distancePc != null)
        _KeyValueLine(
          label: 'Distance',
          value: '${details.distancePc!.toStringAsFixed(1)} pc',
        ),
      if (details.discoveryYear != null)
        _KeyValueLine(label: 'Discovered', value: '${details.discoveryYear}'),
    ];

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _SectionLabel('Public catalogues'),
        const SizedBox(height: 8),
        if (rows.isEmpty)
          const _EmptyLine('No public catalogue fields returned.')
        else
          ...rows,
        if (details.publicSources.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            details.publicSources.join(' · '),
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 10,
              color: BSTheme.ink3,
            ),
          ),
        ],
      ],
    );
  }
}

class _RecentObservationsPanel extends StatelessWidget {
  const _RecentObservationsPanel({
    required this.obs,
    required this.myObservationsOnly,
    required this.onMyObservationsOnlyChanged,
    this.maxRows = 6,
  });

  final List<Observation> obs;
  final bool myObservationsOnly;
  final ValueChanged<bool> onMyObservationsOnlyChanged;
  final int maxRows;

  @override
  Widget build(BuildContext context) {
    return _OpsPanel(
      padding: EdgeInsets.zero,
      child: _PanelScrollBody(
        header: _WorkbenchHeader(
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
        bodyPadding: EdgeInsets.zero,
        body: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const _ObservationHeaderRow(),
            if (obs.isEmpty)
              const Padding(
                padding: EdgeInsets.all(16),
                child: _EmptyLine('No measurements yet today.'),
              )
            else
              ...obs.take(maxRows).map((o) => _ObservationTableRow(obs: o)),
          ],
        ),
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
      height: 44,
      padding: const EdgeInsets.symmetric(horizontal: 14),
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
              fontSize: 14,
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
      height: 30,
      padding: const EdgeInsets.symmetric(horizontal: 16),
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
  const _PlanTimelineRow({
    required this.item,
    required this.selected,
    required this.onTap,
  });

  final TimelineItem item;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final duration = item.estimatedMinutes;
    final st = item.state.toLowerCase();
    String status;
    if (st == 'observing') {
      status = 'In progress';
    } else if (st == 'complete') {
      status = 'Complete';
    } else if (selected) {
      status = 'Selected';
    } else {
      status = 'Pending';
    }
    final type = _timelineType(item);
    final isActive = selected || st == 'observing';
    return Material(
      color: isActive ? BSTheme.sky.withValues(alpha: 0.08) : BSTheme.surface,
      child: InkWell(
        onTap: onTap,
        child: Container(
          constraints: BoxConstraints(minHeight: isActive ? 84 : 58),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          decoration: const BoxDecoration(
            border: Border(bottom: BorderSide(color: BSTheme.glassBorder)),
          ),
          child: Row(
            children: [
              SizedBox(
                width: 20,
                child: LiveDot(
                  color: isActive ? BSTheme.sky : BSTheme.ink3,
                  size: isActive ? 7 : 5,
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
                        fontSize: 14,
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
                      const SizedBox(height: 6),
                      Text(
                        '${duration.toStringAsFixed(0)} min planned',
                        style: const TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 10,
                          color: BSTheme.ink3,
                        ),
                      ),
                    ],
                  ],
                ),
              ),
              Expanded(
                flex: 18,
                child: Text(
                  type,
                  style: const TextStyle(
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
                    color: isActive ? BSTheme.sky : BSTheme.ink3,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

String _timelineType(TimelineItem item) {
  final note = item.notes.toLowerCase();
  final match = RegExp(r'type=([a-z0-9_-]+)').firstMatch(note);
  if (match != null) return match.group(1)!.trim().toUpperCase();
  if (item.explanation.containsKey('transit')) return 'EXOPLANET';
  return 'TARGET';
}

class _ObservationHeaderRow extends StatelessWidget {
  const _ObservationHeaderRow();

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 30,
      padding: const EdgeInsets.symmetric(horizontal: 16),
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
      constraints: const BoxConstraints(minHeight: 44),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 7),
      decoration: const BoxDecoration(
        color: BSTheme.surface,
        border: Border(bottom: BorderSide(color: BSTheme.glassBorder)),
      ),
      child: Row(
        children: [
          Expanded(flex: 18, child: _TableText(_shortDate(obs.receivedAt))),
          Expanded(flex: 28, child: _TableText(obs.targetName, strong: true)),
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
  const _KeyValueLine({required this.label, required this.value, this.color});

  final String label;
  final String value;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            flex: 2,
            child: Text(
              label,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                color: BSTheme.ink3,
              ),
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            flex: 3,
            child: Text(
              value,
              textAlign: TextAlign.end,
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                fontWeight: FontWeight.w800,
                color: color ?? BSTheme.ink2,
                height: 1.35,
              ),
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
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: BSTheme.surface2,
        borderRadius: BorderRadius.circular(4),
        border: Border.all(
          color: clear
              ? BSTheme.glassBorder
              : BSTheme.danger.withValues(alpha: 0.32),
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
            color: clear ? BSTheme.ink3 : BSTheme.danger,
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

/// Panel header + body. Scrolls the body when the panel has a bounded height
/// (wide dashboard); grows naturally on the mobile scroll view.
class _PanelScrollBody extends StatelessWidget {
  const _PanelScrollBody({
    required this.header,
    required this.body,
    this.bodyPadding = const EdgeInsets.all(14),
  });

  final Widget header;
  final Widget body;
  final EdgeInsets bodyPadding;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final bounded =
            constraints.hasBoundedHeight && constraints.maxHeight.isFinite;
        if (!bounded) {
          return Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              header,
              Padding(padding: bodyPadding, child: body),
            ],
          );
        }
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            header,
            Expanded(
              child: SingleChildScrollView(padding: bodyPadding, child: body),
            ),
          ],
        );
      },
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
    return ClipRRect(
      borderRadius: BorderRadius.circular(4),
      child: Stack(
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
          Positioned(left: 0, top: 0, child: _CornerMark(color: accent)),
          Positioned(
            right: 0,
            bottom: 0,
            child: Transform.rotate(
              angle: 3.14159,
              child: _CornerMark(color: accent),
            ),
          ),
        ],
      ),
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
        'exoplanet_transits' => BSTheme.accent,
        'transient_follow_up' => BSTheme.danger,
        _ => BSTheme.warm,
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
                final color =
                    program.isEmpty ? BSTheme.ink2 : _programColor(program);
                return Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: GestureDetector(
                    onTap: () => setState(() => _selectedProgram = program),
                    child: AnimatedContainer(
                      duration: const Duration(milliseconds: 180),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 5,
                      ),
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
                style: const TextStyle(
                  fontFamily: 'Geist',
                  color: BSTheme.ink3,
                ),
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
                final typeLabel =
                    t.targetType.isEmpty ? '—' : t.targetType.toUpperCase();

                return GestureDetector(
                  onTap: () => Navigator.of(context).push(
                    MaterialPageRoute<void>(
                      builder: (_) => TargetDetailScreen(targetName: t.name),
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

