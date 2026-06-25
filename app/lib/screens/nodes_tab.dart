import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';

/// Lists the member's claimed telescope nodes and lets them claim a new one.
class NodesTab extends StatefulWidget {
  const NodesTab({super.key});

  @override
  State<NodesTab> createState() => _NodesTabState();
}

class _NodesTabState extends State<NodesTab> {
  late Future<List<Node>> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<List<Node>> _load() {
    final state = context.read<AppState>();
    return state.api.nodes().catchError((e) {
      state.handleAuthError(e);
      throw e;
    });
  }

  Future<void> _refresh() async => setState(() => _future = _load());

  Future<void> _claimDialog() async {
    final claimed = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      builder: (_) => const _ClaimSheet(),
    );
    if (claimed == true) _refresh();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: AsyncView<List<Node>>(
        future: _future,
        onRefresh: _refresh,
        isEmpty: (list) => list.isEmpty,
        emptyMessage: 'No telescopes yet.\nTap + to connect one.',
        builder: (context, nodes) => ListView.builder(
          padding: const EdgeInsets.all(16),
          itemCount: nodes.length,
          itemBuilder: (context, i) => _NodeCard(node: nodes[i]),
        ),
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _claimDialog,
        icon: const Icon(Icons.add),
        label: const Text('Connect telescope'),
      ),
    );
  }
}

class _NodeCard extends StatelessWidget {
  const _NodeCard({required this.node});
  final Node node;

  @override
  Widget build(BuildContext context) {
    final online = node.online;
    final statusColor = online ? BSTheme.success : BSTheme.danger;
    final statusText = online ? 'Online' : 'Offline';
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Icon(Icons.satellite_alt, size: 34, color: statusColor),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    node.telescopeModel.isEmpty ? 'Telescope' : node.telescopeModel,
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                  const SizedBox(height: 2),
                  Text(node.location,
                      style: Theme.of(context).textTheme.bodyMedium),
                  Text('ID: ${node.nodeId}',
                      style: Theme.of(context).textTheme.bodySmall),
                ],
              ),
            ),
            // Status uses an icon + text, never colour alone (accessibility).
            Semantics(
              label: statusText,
              child: Column(
                children: [
                  Icon(online ? Icons.check_circle : Icons.cancel,
                      color: statusColor, size: 20),
                  const SizedBox(height: 4),
                  Text(statusText,
                      style: TextStyle(
                          color: statusColor, fontWeight: FontWeight.w700)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Bottom sheet that generates a one-time activation code the member types
/// into the node installer to link the telescope to their account.
class _ClaimSheet extends StatefulWidget {
  const _ClaimSheet();

  @override
  State<_ClaimSheet> createState() => _ClaimSheetState();
}

class _ClaimSheetState extends State<_ClaimSheet> {
  String? _code;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _generate();
  }

  Future<void> _generate() async {
    setState(() {
      _busy = true;
      _error = null;
      _code = null;
    });
    try {
      final code = await context.read<AppState>().api.generateActivationCode();
      if (mounted) setState(() { _busy = false; _code = code; });
    } catch (e) {
      if (mounted) setState(() { _busy = false; _error = '$e'; });
    }
  }

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    return Padding(
      padding: EdgeInsets.only(
        left: 24,
        right: 24,
        top: 28,
        bottom: MediaQuery.of(context).viewInsets.bottom + 28,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Connect a telescope', style: tt.headlineSmall),
          const SizedBox(height: 10),
          Text(
            'During node setup, enter this code when prompted. '
            'The telescope will appear here automatically once registered.',
            style: tt.bodyMedium,
          ),
          const SizedBox(height: 24),

          if (_busy)
            const Center(child: CircularProgressIndicator())
          else if (_error != null) ...[
            Text(_error!, style: TextStyle(color: BSTheme.danger)),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              onPressed: _generate,
              icon: const Icon(Icons.refresh),
              label: const Text('Try again'),
            ),
          ] else if (_code != null) ...[
            // Code display
            Container(
              decoration: BoxDecoration(
                color: Theme.of(context).colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(10),
              ),
              padding: const EdgeInsets.symmetric(vertical: 18, horizontal: 20),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      _code!,
                      style: tt.headlineMedium?.copyWith(
                        fontFamily: 'monospace',
                        letterSpacing: 2,
                        fontWeight: FontWeight.w700,
                      ),
                      textAlign: TextAlign.center,
                    ),
                  ),
                  IconButton(
                    tooltip: 'Copy',
                    icon: const Icon(Icons.copy_outlined),
                    onPressed: () async {
                      await Clipboard.setData(ClipboardData(text: _code!));
                      if (context.mounted) {
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(content: Text('Code copied')),
                        );
                      }
                    },
                  ),
                ],
              ),
            ),
            const SizedBox(height: 12),
            Text(
              'Valid for 30 days. Once the node registers, '
              'pull to refresh the telescope list.',
              style: tt.bodySmall,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 20),
            OutlinedButton.icon(
              onPressed: _generate,
              icon: const Icon(Icons.refresh),
              label: const Text('Generate new code'),
            ),
          ],
          const SizedBox(height: 8),
        ],
      ),
    );
  }

}
