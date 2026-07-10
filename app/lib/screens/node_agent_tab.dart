import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api/node_agent_client.dart';
import '../state/app_state.dart';
import '../theme.dart';
import 'nodes_tab.dart' show showClaimSheet;

/// The local control surface for the always-on Python node service.
class NodeAgentTab extends StatefulWidget {
  const NodeAgentTab({super.key});

  @override
  State<NodeAgentTab> createState() => _NodeAgentTabState();
}

class _NodeAgentTabState extends State<NodeAgentTab> {
  final _client = NodeAgentClient();
  Timer? _poller;
  NodeAgentStatus? _status;
  String? _error;

  @override
  void initState() {
    super.initState();
    _refresh();
    _poller = Timer.periodic(const Duration(seconds: 10), (_) => _refresh());
  }

  @override
  void dispose() {
    _poller?.cancel();
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      final status = await _client.status();
      if (mounted) setState(() { _status = status; _error = null; });
    } on NodeAgentException catch (error) {
      if (mounted) setState(() { _error = error.message; _status = null; });
    } catch (_) {
      if (mounted) {
        setState(() {
          _error = 'The local Telescope Net service is not running.';
          _status = null;
        });
      }
    }
  }

  Future<void> _connectTelescope() async {
    final ok = await showClaimSheet(context);
    if (ok && mounted) {
      context.read<AppState>().refreshNodes();
      await _refresh();
    }
  }

  @override
  Widget build(BuildContext context) {
    final status = _status;
    return RefreshIndicator(
      onRefresh: _refresh,
      child: ListView(
        padding: EdgeInsets.fromLTRB(
          20,
          MediaQuery.of(context).padding.top + kToolbarHeight + 20,
          20,
          120,
        ),
        children: [
          Text('LOCAL NODE', style: Theme.of(context).textTheme.labelSmall),
          const SizedBox(height: 6),
          Text(
            'Telescope control',
            style: Theme.of(context).textTheme.headlineMedium,
          ),
          const SizedBox(height: 8),
          Text(
            'This window is your control surface. The node service keeps running '
            'in the background even when you close this app.',
            style: Theme.of(context).textTheme.bodyMedium,
          ),
          const SizedBox(height: 20),
          if (status == null)
            _OfflineCard(error: _error, onRetry: _refresh)
          else ...[
            _StatusCard(status: status),
            const SizedBox(height: 12),
            _MetricGrid(status: status),
            if (!status.cloudRegistered) ...[
              const SizedBox(height: 20),
              FilledButton.icon(
                onPressed: _connectTelescope,
                icon: const Icon(Icons.link),
                label: const Text('Connect telescope'),
              ),
              const SizedBox(height: 8),
              Text(
                'Sign-in is already done. Connect telescope links this computer '
                'to your account — no activation code.',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
          ],
        ],
      ),
    );
  }
}

class _OfflineCard extends StatelessWidget {
  const _OfflineCard({required this.error, required this.onRetry});
  final String? error;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) => Card(
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Icon(Icons.cloud_off_outlined, color: BSTheme.danger),
              const SizedBox(height: 12),
              Text(
                'Node service unavailable',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const SizedBox(height: 6),
              Text(
                error ?? 'Checking local service…',
                style: Theme.of(context).textTheme.bodySmall,
              ),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                onPressed: onRetry,
                icon: const Icon(Icons.refresh),
                label: const Text('Check again'),
              ),
            ],
          ),
        ),
      );
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({required this.status});
  final NodeAgentStatus status;

  @override
  Widget build(BuildContext context) {
    final online = status.connected && status.telescopeConnected;
    final color = online ? BSTheme.success : BSTheme.warm;
    final title = status.cloudRegistered
        ? (online ? 'Node online' : 'Setup or reconnect needed')
        : 'Not linked to your account yet';
    final detail = !status.cloudRegistered
        ? 'Tap Connect telescope to link this computer to your account.'
        : status.commissioningStatus == null
            ? 'Local service responding.'
            : 'Commissioning: ${status.commissioningStatus}.';
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Row(
          children: [
            Icon(
              online ? Icons.check_circle_outline : Icons.info_outline,
              color: color,
              size: 30,
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(title, style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 4),
                  Text(detail, style: Theme.of(context).textTheme.bodySmall),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _MetricGrid extends StatelessWidget {
  const _MetricGrid({required this.status});
  final NodeAgentStatus status;

  @override
  Widget build(BuildContext context) => Wrap(
        spacing: 10,
        runSpacing: 10,
        children: [
          _Metric(
            label: 'Account',
            value: status.cloudRegistered ? 'Linked' : 'Not linked',
          ),
          _Metric(
            label: 'Telescope',
            value: status.telescopeConnected ? 'Connected' : 'Offline',
          ),
          _Metric(
            label: 'Camera',
            value: status.cameraConnected ? 'Connected' : 'Offline',
          ),
          _Metric(
            label: 'Safety',
            value: status.safe
                ? 'Safe'
                : (status.safetyReason.isEmpty ? 'Paused' : status.safetyReason),
          ),
          _Metric(
            label: 'Photometry',
            value: status.photometryRunning
                ? 'Processing'
                : '${status.queuedFrames} queued',
          ),
        ],
      );
}

class _Metric extends StatelessWidget {
  const _Metric({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => SizedBox(
        width: 170,
        child: Card(
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label.toUpperCase(),
                  style: Theme.of(context).textTheme.labelSmall,
                ),
                const SizedBox(height: 7),
                Text(
                  value,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.titleSmall,
                ),
              ],
            ),
          ),
        ),
      );
}
