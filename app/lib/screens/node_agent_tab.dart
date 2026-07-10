import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/node_agent_client.dart';
import '../theme.dart';

/// The local control surface for the always-on Python node service.
class NodeAgentTab extends StatefulWidget {
  const NodeAgentTab({super.key});

  @override
  State<NodeAgentTab> createState() => _NodeAgentTabState();
}

class _NodeAgentTabState extends State<NodeAgentTab> {
  final _client = NodeAgentClient();
  final _codeCtrl = TextEditingController();
  Timer? _poller;
  NodeAgentStatus? _status;
  String? _error;
  String? _activateError;
  bool _activating = false;
  bool _showActivateForm = false;

  @override
  void initState() {
    super.initState();
    _refresh();
    _poller = Timer.periodic(const Duration(seconds: 10), (_) => _refresh());
  }

  @override
  void dispose() {
    _poller?.cancel();
    _codeCtrl.dispose();
    super.dispose();
  }

  Future<void> _refresh() async {
    try {
      final status = await _client.status();
      if (mounted) {
        setState(() {
          _status = status;
          _error = null;
          if (status.cloudRegistered) _showActivateForm = false;
        });
      }
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

  Future<void> _activate() async {
    setState(() {
      _activating = true;
      _activateError = null;
    });
    try {
      await _client.activate(_codeCtrl.text);
      if (!mounted) return;
      setState(() {
        _activating = false;
        _showActivateForm = false;
        _codeCtrl.clear();
      });
      await _refresh();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Telescope linked to your account.')),
        );
      }
    } on NodeAgentException catch (e) {
      if (mounted) {
        setState(() {
          _activating = false;
          _activateError = e.message;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _activating = false;
          _activateError = '$e';
        });
      }
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
              if (!_showActivateForm)
                FilledButton.icon(
                  onPressed: () => setState(() => _showActivateForm = true),
                  icon: const Icon(Icons.key_outlined),
                  label: const Text('Enter activation code'),
                )
              else
                _ActivateCard(
                  controller: _codeCtrl,
                  busy: _activating,
                  error: _activateError,
                  onSubmit: _activate,
                  onCancel: () => setState(() {
                    _showActivateForm = false;
                    _activateError = null;
                  }),
                ),
            ],
          ],
        ],
      ),
    );
  }
}

class _ActivateCard extends StatelessWidget {
  const _ActivateCard({
    required this.controller,
    required this.busy,
    required this.error,
    required this.onSubmit,
    required this.onCancel,
  });

  final TextEditingController controller;
  final bool busy;
  final String? error;
  final VoidCallback onSubmit;
  final VoidCallback onCancel;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              'Link this computer',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 6),
            Text(
              'Paste the activation code from Telescopes → Connect telescope. '
              'Only open this form when you are ready to enter a code.',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 14),
            TextField(
              controller: controller,
              enabled: !busy,
              textCapitalization: TextCapitalization.characters,
              inputFormatters: [
                FilteringTextInputFormatter.allow(RegExp(r'[A-Za-z0-9\-]')),
              ],
              decoration: const InputDecoration(
                labelText: 'Activation code',
                hintText: 'BS-2024-XXXXXXXX',
                border: OutlineInputBorder(),
              ),
              onSubmitted: (_) => onSubmit(),
            ),
            if (error != null) ...[
              const SizedBox(height: 10),
              Text(error!, style: const TextStyle(color: BSTheme.danger)),
            ],
            const SizedBox(height: 14),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton(
                    onPressed: busy ? null : onCancel,
                    child: const Text('Cancel'),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: FilledButton(
                    onPressed: busy ? null : onSubmit,
                    child: busy
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Text('Connect'),
                  ),
                ),
              ],
            ),
          ],
        ),
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
        ? 'Tap “Enter activation code” when you have a code from Connect telescope.'
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
