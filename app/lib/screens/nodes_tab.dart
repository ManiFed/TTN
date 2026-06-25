// ignore: avoid_web_libraries_in_flutter
import 'dart:html' as html;

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
    final top = MediaQuery.of(context).padding.top + kToolbarHeight;
    final bottom = MediaQuery.of(context).padding.bottom + 64;

    return Stack(
      children: [
        AsyncView<List<Node>>(
          future: _future,
          onRefresh: _refresh,
          isEmpty: (list) => list.isEmpty,
          emptyMessage: 'No telescopes yet.\nTap + to connect one.',
          builder: (context, nodes) => ListView.builder(
            padding: EdgeInsets.fromLTRB(16, top + 8, 16, bottom + 80),
            itemCount: nodes.length,
            itemBuilder: (context, i) => _NodeCard(node: nodes[i]),
          ),
        ),
        Positioned(
          right: 16,
          bottom: bottom + 16,
          child: FloatingActionButton.extended(
            onPressed: _claimDialog,
            icon: const Icon(Icons.add),
            label: const Text('Connect telescope'),
          ),
        ),
      ],
    );
  }
}

// ── Node card — instrument panel aesthetic ────────────────────────────────────

class _NodeCard extends StatelessWidget {
  const _NodeCard({required this.node});
  final Node node;

  @override
  Widget build(BuildContext context) {
    final online = node.online;
    final statusColor = online ? BSTheme.success : BSTheme.danger;
    final statusLabel = online ? 'ONLINE' : 'OFFLINE';

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(20),
        color: BSTheme.glassBg,
        border: Border.all(
          color: online
              ? BSTheme.success.withValues(alpha: 0.28)
              : BSTheme.glassBorder,
        ),
      ),
      child: Row(
        children: [
          // Icon + pulsing status dot
          Column(
            children: [
              Container(
                width: 48,
                height: 48,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: statusColor.withValues(alpha: 0.10),
                  border: Border.all(color: statusColor.withValues(alpha: 0.28)),
                ),
                child: Icon(Icons.satellite_alt, size: 22, color: statusColor),
              ),
              const SizedBox(height: 6),
              _StatusDot(online: online),
            ],
          ),
          const SizedBox(width: 16),
          // Details
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  node.telescopeModel.isEmpty
                      ? 'Telescope'
                      : node.telescopeModel,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 16,
                    fontWeight: FontWeight.w600,
                    letterSpacing: -0.4,
                    color: BSTheme.ink,
                  ),
                ),
                if (node.location.isNotEmpty) ...[
                  const SizedBox(height: 3),
                  Row(
                    children: [
                      const Icon(
                        Icons.location_on_outlined,
                        size: 12,
                        color: BSTheme.ink3,
                      ),
                      const SizedBox(width: 3),
                      Flexible(
                        child: Text(
                          node.location,
                          style: const TextStyle(
                            fontFamily: 'Geist',
                            fontSize: 12,
                            color: BSTheme.ink3,
                          ),
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                    ],
                  ),
                ],
                const SizedBox(height: 8),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(6),
                    color: const Color(0x0BA0B9FF),
                    border: Border.all(color: BSTheme.glassBorder),
                  ),
                  child: Text(
                    node.nodeId,
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 10,
                      fontWeight: FontWeight.w500,
                      letterSpacing: 0.8,
                      color: BSTheme.ink3,
                    ),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(width: 12),
          Semantics(
            label: statusLabel,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(8),
                color: statusColor.withValues(alpha: 0.12),
                border: Border.all(color: statusColor.withValues(alpha: 0.3)),
              ),
              child: Text(
                statusLabel,
                style: TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 10,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 1.2,
                  color: statusColor,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// Tiny dot that pulses when online.
class _StatusDot extends StatefulWidget {
  const _StatusDot({required this.online});
  final bool online;

  @override
  State<_StatusDot> createState() => _StatusDotState();
}

class _StatusDotState extends State<_StatusDot>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;
  late final Animation<double> _anim;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1400),
    );
    if (widget.online) _ctrl.repeat(reverse: true);
    _anim = Tween<double>(begin: 0.3, end: 1.0).animate(
      CurvedAnimation(parent: _ctrl, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final color = widget.online ? BSTheme.success : BSTheme.danger;
    return AnimatedBuilder(
      animation: _anim,
      builder: (_, __) => Container(
        width: 6,
        height: 6,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: color,
          boxShadow: widget.online
              ? [
                  BoxShadow(
                    color: color.withValues(alpha: _anim.value * 0.9),
                    blurRadius: _anim.value * 8,
                    spreadRadius: _anim.value,
                  ),
                ]
              : null,
        ),
      ),
    );
  }
}

// ── Claim sheet ───────────────────────────────────────────────────────────────

/// Bottom sheet that generates a one-time activation code the member types
/// into the node installer to link the telescope to their account.
class _ClaimSheet extends StatefulWidget {
  const _ClaimSheet();

  @override
  State<_ClaimSheet> createState() => _ClaimSheetState();
}

class _ClaimSheetState extends State<_ClaimSheet> {
  final _locationCtrl = TextEditingController();
  String? _code;
  bool _busy = false;
  bool _locating = false;
  String? _error;
  double? _lat;
  double? _lon;

  @override
  void dispose() {
    _locationCtrl.dispose();
    super.dispose();
  }

  Future<void> _detectLocation() async {
    setState(() { _locating = true; _error = null; });
    try {
      final pos = await html.window.navigator.geolocation.getCurrentPosition(
        enableHighAccuracy: true,
      );
      final lat = (pos.coords!.latitude as num).toDouble();
      final lon = (pos.coords!.longitude as num).toDouble();
      if (mounted) {
        setState(() {
          _lat = lat;
          _lon = lon;
          _locating = false;
          // Pre-fill name field if empty
          if (_locationCtrl.text.trim().isEmpty) {
            _locationCtrl.text =
                '${lat.toStringAsFixed(4)}°, ${lon.toStringAsFixed(4)}°';
          }
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _locating = false;
          _error = 'Location access denied. Enter a location name instead.';
        });
      }
    }
  }

  Future<void> _generate() async {
    final location = _locationCtrl.text.trim();
    if (_lat == null && location.isEmpty) {
      setState(() => _error = 'Enter a location or tap the GPS button.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
      _code = null;
    });
    try {
      final code = await context
          .read<AppState>()
          .api
          .generateActivationCode(
            locationName: location.isEmpty ? null : location,
            lat: _lat,
            lon: _lon,
          );
      if (mounted) {
        setState(() {
          _busy = false;
          _code = code;
        });
      }
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
          // Drag handle
          Center(
            child: Container(
              width: 36,
              height: 4,
              margin: const EdgeInsets.only(bottom: 20),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(2),
                color: BSTheme.glassBorder,
              ),
            ),
          ),
          Text('Connect a telescope', style: tt.headlineSmall),
          const SizedBox(height: 10),
          Text(
            'Enter the location of the telescope — used for scheduling and sky '
            'conditions. Tap the GPS button to detect automatically.',
            style: tt.bodyMedium,
          ),
          const SizedBox(height: 20),
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: TextField(
                  controller: _locationCtrl,
                  enabled: !_busy && _code == null,
                  decoration: InputDecoration(
                    labelText: 'Location',
                    hintText: 'e.g. Starfront Observatories, Rockwood TX',
                    border: const OutlineInputBorder(),
                    prefixIcon: const Icon(Icons.location_on_outlined),
                    suffixIcon: _lat != null
                        ? const Tooltip(
                            message: 'GPS coordinates detected',
                            child: Icon(Icons.gps_fixed,
                                color: Colors.green, size: 18),
                          )
                        : null,
                  ),
                  textInputAction: TextInputAction.done,
                  onSubmitted: (_) => _generate(),
                ),
              ),
              const SizedBox(width: 8),
              Tooltip(
                message: 'Detect my location',
                child: FilledButton.tonal(
                  onPressed: (_busy || _code != null || _locating)
                      ? null
                      : _detectLocation,
                  style: FilledButton.styleFrom(
                    minimumSize: const Size(48, 56),
                    padding: EdgeInsets.zero,
                  ),
                  child: _locating
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.my_location),
                ),
              ),
            ],
          ),
          if (_lat != null) ...[
            const SizedBox(height: 6),
            Text(
              'GPS: ${_lat!.toStringAsFixed(5)}°, ${_lon!.toStringAsFixed(5)}°',
              style: const TextStyle(fontSize: 11, color: Colors.green),
            ),
          ],
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(
              _error!,
              style: const TextStyle(color: BSTheme.danger, fontSize: 13),
            ),
          ],
          const SizedBox(height: 16),
          if (_busy)
            const Center(child: CircularProgressIndicator())
          else if (_code == null)
            FilledButton(
              onPressed: _generate,
              child: const Text('Get activation code'),
            )
          else ...[
            Text(
              'Enter this code in the node software to activate your telescope:',
              style: tt.bodySmall,
            ),
            const SizedBox(height: 6),
            Text(
              '1. On the node computer, open a browser to http://localhost:5173\n'
              '2. Go to Settings → Cloud tab\n'
              '3. Paste the code and save — the telescope will appear here.',
              style: tt.bodySmall?.copyWith(
                fontFamily: 'monospace',
                height: 1.6,
              ),
            ),
            const SizedBox(height: 16),
            Container(
              decoration: BoxDecoration(
                color: Theme.of(context).colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(10),
              ),
              padding:
                  const EdgeInsets.symmetric(vertical: 18, horizontal: 20),
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
            const SizedBox(height: 8),
            Text(
              'Valid for 30 days. Once the node registers, '
              'pull to refresh the telescope list.',
              style: tt.bodySmall,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              onPressed: () => setState(() {
                _code = null;
                _error = null;
              }),
              icon: const Icon(Icons.refresh),
              label: const Text('Start over'),
            ),
          ],
          const SizedBox(height: 8),
        ],
      ),
    );
  }
}
