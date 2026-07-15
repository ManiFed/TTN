import 'package:flutter/material.dart';

import '../theme.dart';
import 'models.dart';

/// Human-readable operational status derived from cloud node + plan data.
class NodeLiveStatus {
  const NodeLiveStatus({
    required this.headline,
    required this.detail,
    required this.color,
    required this.icon,
    this.severity = NodeStatusSeverity.info,
  });

  final String headline;
  final String detail;
  final Color color;
  final IconData icon;
  final NodeStatusSeverity severity;
}

enum NodeStatusSeverity { ok, info, warn, error }

/// Pick the single most important status line for the Tonight banner.
NodeLiveStatus primaryNodeStatus({
  required Node? node,
  int planCount = 0,
  String? activePlanTarget,
}) {
  if (node == null) {
    return const NodeLiveStatus(
      headline: 'No telescope connected',
      detail: 'Connect a node to start observing with the network.',
      color: BSTheme.ink3,
      icon: Icons.link_off,
      severity: NodeStatusSeverity.warn,
    );
  }

  if (node.isOnVacation) {
    final until = node.vacationUntil.isNotEmpty ? node.vacationUntil : 'later';
    return NodeLiveStatus(
      headline: 'On vacation',
      detail: '${node.label} is paused until $until.',
      color: BSTheme.warm,
      icon: Icons.beach_access,
      severity: NodeStatusSeverity.info,
    );
  }

  if (node.portable && node.isSleeping) {
    return NodeLiveStatus(
      headline: 'Sleeping',
      detail: 'Tap Start tonight on ${node.label} to begin this session.',
      color: BSTheme.accent,
      icon: Icons.bedtime_outlined,
      severity: NodeStatusSeverity.info,
    );
  }

  if (!node.online) {
    return NodeLiveStatus(
      headline: 'Telescope offline',
      detail:
          '${node.label} has not checked in recently. Check power, network, and that the Node Agent is running.',
      color: BSTheme.danger,
      icon: Icons.cloud_off,
      severity: NodeStatusSeverity.error,
    );
  }

  final c = node.conditions;

  if (c.telescopeConnected == false) {
    return const NodeLiveStatus(
      headline: 'No scope connected',
      detail:
          'The Node Agent is online but cannot reach the telescope. Check USB, Wi‑Fi, and ALPACA.',
      color: BSTheme.danger,
      icon: Icons.settings_input_antenna,
      severity: NodeStatusSeverity.error,
    );
  }

  if (c.safe == false) {
    final reason = c.reason.toLowerCase();
    // The node agent parks the telescope and tags the reason with the raw
    // Unix signal name whenever its process is restarted (a reinstall, an
    // update, the computer sleeping) -- that's routine, not a fault, and a
    // signal name should never reach a member as-is.
    if (reason.contains('sigterm') || reason.contains('sigint')) {
      return const NodeLiveStatus(
        headline: 'Restarting',
        detail: 'The node software just restarted and parked the telescope '
            'as a precaution. It will resume automatically within a minute.',
        color: BSTheme.warm,
        icon: Icons.refresh,
        severity: NodeStatusSeverity.info,
      );
    }
    if (reason.contains('dawn')) {
      final sun = c.sunElevation;
      final threshold = c.dawnThreshold;
      final sunText = sun != null ? '${sun.toStringAsFixed(1)}°' : 'above threshold';
      final needText = threshold != null
          ? 'Astronomical night begins below ${threshold.toStringAsFixed(0)}°.'
          : 'Waiting for astronomical darkness.';
      return NodeLiveStatus(
        headline: 'Not dark enough yet',
        detail: 'Sun at $sunText. $needText Observing starts automatically once it is safe.',
        color: BSTheme.warm,
        icon: Icons.wb_twilight,
        severity: NodeStatusSeverity.warn,
      );
    }
    return NodeLiveStatus(
      headline: 'Blocked for safety',
      detail: c.reason.isNotEmpty
          ? c.reason
          : 'The node will resume when conditions are safe.',
      color: BSTheme.danger,
      icon: Icons.shield_outlined,
      severity: NodeStatusSeverity.error,
    );
  }

  if (c.scheduleRunning) {
    final target = c.scheduleTarget.isNotEmpty
        ? c.scheduleTarget
        : (activePlanTarget ?? 'target');
    final phase = c.schedulePhase.toLowerCase();
    if (phase == 'slewing') {
      return NodeLiveStatus(
        headline: 'Slewing',
        detail: '${node.label} is moving to $target.',
        color: BSTheme.sky,
        icon: Icons.explore,
        severity: NodeStatusSeverity.ok,
      );
    }
    if (phase == 'exposing' && c.scheduleFrames > 0) {
      return NodeLiveStatus(
        headline: 'Observing $target',
        detail:
            'Frame ${c.scheduleFrame} of ${c.scheduleFrames} on ${node.label}.',
        color: BSTheme.success,
        icon: Icons.radio_button_checked,
        severity: NodeStatusSeverity.ok,
      );
    }
    if (phase == 'exposing') {
      return NodeLiveStatus(
        headline: 'Observing $target',
        detail: '${node.label} is taking exposures now.',
        color: BSTheme.success,
        icon: Icons.radio_button_checked,
        severity: NodeStatusSeverity.ok,
      );
    }
    return NodeLiveStatus(
      headline: 'Running tonight\'s plan',
      detail: c.scheduleTotal > 0
          ? '${c.scheduleCompleted} of ${c.scheduleTotal} assignments on ${node.label}.'
          : '${node.label} is executing the cloud schedule.',
      color: BSTheme.sky,
      icon: Icons.play_circle_outline,
      severity: NodeStatusSeverity.ok,
    );
  }

  if (c.autoRunPlans == false && planCount > 0) {
    return NodeLiveStatus(
      headline: 'Plan ready — auto-run off',
      detail:
          'Tonight\'s plan has $planCount targets but the node will not start until auto-run is enabled in Node Agent settings.',
      color: BSTheme.warm,
      icon: Icons.pause_circle_outline,
      severity: NodeStatusSeverity.warn,
    );
  }

  if (planCount == 0) {
    return NodeLiveStatus(
      headline: 'Waiting for tonight\'s plan',
      detail:
          '${node.label} is online and ready. The network will assign targets when planning completes.',
      color: BSTheme.accent,
      icon: Icons.hourglass_top,
      severity: NodeStatusSeverity.info,
    );
  }

  return NodeLiveStatus(
    headline: 'Ready to observe',
    detail:
        '$planCount targets queued on ${node.label}. Observing will start automatically.',
    color: BSTheme.success,
    icon: Icons.check_circle_outline,
    severity: NodeStatusSeverity.ok,
  );
}

String heartbeatAgeLabel(String iso) {
  final parsed = DateTime.tryParse(iso);
  if (parsed == null) return iso.isEmpty ? '—' : iso;
  final diff = DateTime.now().difference(parsed.toLocal());
  if (diff.inSeconds < 60) return 'just now';
  if (diff.inMinutes < 60) return '${diff.inMinutes}m ago';
  if (diff.inHours < 24) return '${diff.inHours}h ago';
  return '${diff.inDays}d ago';
}