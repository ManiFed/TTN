import 'package:flutter/material.dart';
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

/// Bottom sheet to claim a node by its node ID + API key (printed at install).
class _ClaimSheet extends StatefulWidget {
  const _ClaimSheet();

  @override
  State<_ClaimSheet> createState() => _ClaimSheetState();
}

class _ClaimSheetState extends State<_ClaimSheet> {
  final _formKey = GlobalKey<FormState>();
  final _nodeId = TextEditingController();
  final _apiKey = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _nodeId.dispose();
    _apiKey.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await context.read<AppState>().api.claimNode(
            _nodeId.text.trim(),
            _apiKey.text.trim(),
          );
      if (mounted) Navigator.of(context).pop(true);
    } catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = '$e';
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        left: 20,
        right: 20,
        top: 24,
        bottom: MediaQuery.of(context).viewInsets.bottom + 24,
      ),
      child: Form(
        key: _formKey,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text('Connect a telescope',
                style: Theme.of(context).textTheme.headlineSmall),
            const SizedBox(height: 8),
            Text(
              'Enter the Node ID and key shown when the telescope was set up.',
              style: Theme.of(context).textTheme.bodyMedium,
            ),
            const SizedBox(height: 20),
            TextFormField(
              controller: _nodeId,
              decoration: const InputDecoration(
                labelText: 'Node ID',
                prefixIcon: Icon(Icons.tag),
              ),
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'Required' : null,
            ),
            const SizedBox(height: 16),
            TextFormField(
              controller: _apiKey,
              decoration: const InputDecoration(
                labelText: 'API key',
                prefixIcon: Icon(Icons.key),
              ),
              validator: (v) =>
                  (v == null || v.trim().isEmpty) ? 'Required' : null,
            ),
            if (_error != null) ...[
              const SizedBox(height: 12),
              Text(_error!, style: const TextStyle(color: BSTheme.danger)),
            ],
            const SizedBox(height: 24),
            ElevatedButton(
              onPressed: _busy ? null : _submit,
              child: _busy
                  ? const SizedBox(
                      height: 22,
                      width: 22,
                      child: CircularProgressIndicator(strokeWidth: 2.5),
                    )
                  : const Text('Connect'),
            ),
          ],
        ),
      ),
    );
  }
}
