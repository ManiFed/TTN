// ignore: avoid_web_libraries_in_flutter
import 'dart:convert';
import 'dart:html' as html;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
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

/// Bottom sheet that connects a telescope node to the member's account.
/// Flow: enter location → generate code → enter pairing token → push to node.
class _ClaimSheet extends StatefulWidget {
  const _ClaimSheet();

  @override
  State<_ClaimSheet> createState() => _ClaimSheetState();
}

enum _LocStep { idle, geocoding, confirming, confirmed }

class _ClaimSheetState extends State<_ClaimSheet> {
  final _locationCtrl = TextEditingController();
  final _pairCtrl = TextEditingController();
  String? _code;
  bool _busy = false;
  bool _locating = false;
  String? _error;
  double? _lat;
  double? _lon;
  String? _resolvedLocation;
  bool _pushed = false;
  bool _pushing = false;
  _LocStep _step = _LocStep.idle;

  @override
  void initState() {
    super.initState();
    _locationCtrl.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _locationCtrl.dispose();
    _pairCtrl.dispose();
    super.dispose();
  }

  void _resetLocation() {
    _lat = null;
    _lon = null;
    _resolvedLocation = null;
    _step = _LocStep.idle;
    _error = null;
  }

  Future<void> _detectLocation() async {
    setState(() {
      _locating = true;
      _error = null;
    });
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
          if (_locationCtrl.text.trim().isEmpty) {
            _locationCtrl.text =
                '${lat.toStringAsFixed(4)}°, ${lon.toStringAsFixed(4)}°';
          }
          _step = _LocStep.confirmed;
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

  Future<void> _lookupLocation() async {
    final query = _locationCtrl.text.trim();
    if (query.isEmpty) return;
    setState(() {
      _step = _LocStep.geocoding;
      _error = null;
    });
    try {
      final uri = Uri.https('nominatim.openstreetmap.org', '/search', {
        'q': query,
        'format': 'json',
        'limit': '1',
        'addressdetails': '1',
      });
      final resp = await http.get(
        uri,
        headers: {'User-Agent': 'BoundlessSkiesApp/1.0'},
      );
      if (!mounted) return;
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as List<dynamic>;
        if (data.isNotEmpty) {
          final result = data.first as Map<String, dynamic>;
          final lat = double.tryParse(result['lat'] as String? ?? '');
          final lon = double.tryParse(result['lon'] as String? ?? '');
          if (lat != null && lon != null) {
            setState(() {
              _lat = lat;
              _lon = lon;
              _resolvedLocation = result['display_name'] as String?;
              _step = _LocStep.confirming;
            });
            return;
          }
        }
        setState(() {
          _step = _LocStep.idle;
          _error = 'No location found for "$query". Try a more specific name.';
        });
      } else {
        setState(() {
          _step = _LocStep.idle;
          _error = 'Location lookup failed. Try again or use the GPS button.';
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _step = _LocStep.idle;
          _error = 'Location lookup failed. Try again or use the GPS button.';
        });
      }
    }
  }

  Future<void> _generate() async {
    final location = _locationCtrl.text.trim();
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
          if (_code == null) ..._buildLocationSection(tt),
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(
              _error!,
              style: const TextStyle(color: BSTheme.danger, fontSize: 13),
            ),
          ],
          const SizedBox(height: 16),
          if (_code != null)
            ..._buildCodeSection(tt, context)
          else if (_busy)
            const Center(child: CircularProgressIndicator())
          else if (_step == _LocStep.confirmed)
            FilledButton(
              onPressed: _generate,
              child: const Text('Get activation code'),
            ),
          const SizedBox(height: 8),
        ],
      ),
    );
  }

  List<Widget> _buildLocationSection(TextTheme tt) {
    switch (_step) {
      case _LocStep.idle:
        return [
          Text(
            'Enter the location of the telescope — used for scheduling and '
            'sky conditions.',
            style: tt.bodyMedium,
          ),
          const SizedBox(height: 20),
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: TextField(
                  controller: _locationCtrl,
                  enabled: !_locating,
                  autofocus: true,
                  decoration: const InputDecoration(
                    labelText: 'Location',
                    hintText: 'e.g. Larchmont, NY or Dark Sky Ranch, TX',
                    border: OutlineInputBorder(),
                    prefixIcon: Icon(Icons.location_on_outlined),
                  ),
                  textInputAction: TextInputAction.done,
                  onSubmitted: (_) => _lookupLocation(),
                ),
              ),
              const SizedBox(width: 8),
              Tooltip(
                message: 'Use device GPS instead',
                child: FilledButton.tonal(
                  onPressed: _locating ? null : _detectLocation,
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
          const SizedBox(height: 12),
          FilledButton(
            onPressed: _locationCtrl.text.trim().isEmpty ? null : _lookupLocation,
            child: const Text('Confirm location'),
          ),
        ];

      case _LocStep.geocoding:
        return [
          Text(
            'Enter the location of the telescope — used for scheduling and '
            'sky conditions.',
            style: tt.bodyMedium,
          ),
          const SizedBox(height: 20),
          TextField(
            controller: _locationCtrl,
            enabled: false,
            decoration: const InputDecoration(
              labelText: 'Location',
              border: OutlineInputBorder(),
              prefixIcon: Icon(Icons.location_on_outlined),
            ),
          ),
          const SizedBox(height: 16),
          const Center(child: CircularProgressIndicator()),
          const SizedBox(height: 4),
        ];

      case _LocStep.confirming:
        return [
          Text('Is this the right location?', style: tt.titleMedium),
          const SizedBox(height: 16),
          Container(
            padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 16),
            decoration: BoxDecoration(
              border: Border.all(color: BSTheme.glassBorder),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Padding(
                      padding: EdgeInsets.only(top: 2),
                      child: Icon(Icons.location_on, size: 16, color: BSTheme.accent),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        _resolvedLocation ?? _locationCtrl.text,
                        style: const TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 14,
                          fontWeight: FontWeight.w500,
                          color: BSTheme.ink,
                        ),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Padding(
                  padding: const EdgeInsets.only(left: 24),
                  child: Text(
                    '${_lat!.toStringAsFixed(5)}°, ${_lon!.toStringAsFixed(5)}°',
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 12,
                      color: BSTheme.ink3,
                    ),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: () => setState(_resetLocation),
                  icon: const Icon(Icons.edit_outlined, size: 16),
                  label: const Text('Edit'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: FilledButton.icon(
                  onPressed: () => setState(() => _step = _LocStep.confirmed),
                  icon: const Icon(Icons.check, size: 16),
                  label: const Text('Yes, this is it'),
                ),
              ),
            ],
          ),
        ];

      case _LocStep.confirmed:
        final hasCoords = _lat != null;
        final coordStr = hasCoords
            ? '  ·  ${_lat!.toStringAsFixed(4)}°, ${_lon!.toStringAsFixed(4)}°'
            : '';
        final label = _resolvedLocation ?? _locationCtrl.text;
        return [
          Row(
            children: [
              const Icon(Icons.check_circle_outline, size: 18, color: Colors.green),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  '$label$coordStr',
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    color: BSTheme.ink,
                  ),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (!_busy)
                TextButton(
                  onPressed: () => setState(_resetLocation),
                  style: TextButton.styleFrom(
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                  ),
                  child: const Text('Edit'),
                ),
            ],
          ),
          const SizedBox(height: 4),
        ];
    }
  }

  Future<void> _pushToTelescope() async {
    final token = _pairCtrl.text.trim().toUpperCase();
    if (token.isEmpty) {
      setState(() => _error = 'Enter the pairing token shown in the terminal.');
      return;
    }
    setState(() { _pushing = true; _error = null; });
    try {
      await context.read<AppState>().api.pushActivationCode(token, _code!);
      if (mounted) setState(() { _pushed = true; _pushing = false; });
    } catch (e) {
      if (mounted) setState(() { _pushing = false; _error = '$e'; });
    }
  }

  List<Widget> _buildCodeSection(TextTheme tt, BuildContext context) {
    if (_pushed) {
      return [
        const Icon(Icons.check_circle_outline, color: Colors.green, size: 48),
        const SizedBox(height: 16),
        Text('Telescope connected!', style: tt.titleLarge),
        const SizedBox(height: 8),
        Text(
          'The node software will register within 30 seconds. '
          'Pull to refresh the telescope list.',
          style: tt.bodyMedium,
          textAlign: TextAlign.center,
        ),
        const SizedBox(height: 20),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Done'),
        ),
      ];
    }

    return [
      Text('Enter the pairing token', style: tt.titleMedium),
      const SizedBox(height: 8),
      Text(
        'Look at the terminal window where the node software is running. '
        'You\'ll see a pairing token like NOVA-4827.',
        style: tt.bodyMedium,
      ),
      const SizedBox(height: 20),
      TextField(
        controller: _pairCtrl,
        autofocus: true,
        textCapitalization: TextCapitalization.characters,
        decoration: const InputDecoration(
          labelText: 'Pairing token',
          hintText: 'e.g. NOVA-4827',
          border: OutlineInputBorder(),
          prefixIcon: Icon(Icons.link_outlined),
        ),
        textInputAction: TextInputAction.done,
        onSubmitted: (_) => _pushToTelescope(),
        onChanged: (_) => setState(() {}),
      ),
      const SizedBox(height: 16),
      if (_pushing)
        const Center(child: CircularProgressIndicator())
      else
        FilledButton.icon(
          onPressed: _pairCtrl.text.trim().isEmpty ? null : _pushToTelescope,
          icon: const Icon(Icons.send_outlined, size: 18),
          label: const Text('Connect telescope'),
        ),
      const SizedBox(height: 16),
      OutlinedButton.icon(
        onPressed: () => setState(() {
          _code = null;
          _error = null;
          _pushed = false;
          _pushing = false;
          _pairCtrl.clear();
          _resetLocation();
          _locationCtrl.clear();
        }),
        icon: const Icon(Icons.arrow_back, size: 16),
        label: const Text('Start over'),
      ),
    ];
  }
}
