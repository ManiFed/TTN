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
            itemBuilder: (context, i) =>
                _NodeCard(node: nodes[i], onRefresh: _refresh),
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

// ── Node card ─────────────────────────────────────────────────────────────────

class _NodeCard extends StatelessWidget {
  const _NodeCard({required this.node, required this.onRefresh});
  final Node node;
  final VoidCallback onRefresh;

  Color get _statusColor {
    switch (node.status) {
      case 'active':
        return BSTheme.success;
      case 'sleeping':
        return BSTheme.accent;
      case 'vacation':
        return BSTheme.warm;
      case 'disabled':
        return BSTheme.ink3;
      default:
        return BSTheme.danger;
    }
  }

  String get _statusLabel {
    switch (node.status) {
      case 'active':
        return 'ONLINE';
      case 'sleeping':
        return 'SLEEPING';
      case 'vacation':
        return 'VACATION';
      case 'disabled':
        return 'DISABLED';
      default:
        return 'OFFLINE';
    }
  }

  Color get _borderColor {
    switch (node.status) {
      case 'active':
        return BSTheme.success.withValues(alpha: 0.28);
      case 'sleeping':
        return BSTheme.accent.withValues(alpha: 0.22);
      case 'vacation':
        return BSTheme.warm.withValues(alpha: 0.22);
      default:
        return BSTheme.glassBorder;
    }
  }

  @override
  Widget build(BuildContext context) {
    final color = _statusColor;
    final label = _statusLabel;

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(20),
        color: BSTheme.glassBg,
        border: Border.all(color: _borderColor),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Column(
                children: [
                  Container(
                    width: 48,
                    height: 48,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: color.withValues(alpha: 0.10),
                      border:
                          Border.all(color: color.withValues(alpha: 0.28)),
                    ),
                    child:
                        Icon(Icons.satellite_alt, size: 22, color: color),
                  ),
                  const SizedBox(height: 6),
                  _StatusDot(status: node.status),
                ],
              ),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: Text(
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
                        ),
                        if (node.portable) ...[
                          const SizedBox(width: 8),
                          Container(
                            padding: const EdgeInsets.symmetric(
                                horizontal: 7, vertical: 2),
                            decoration: BoxDecoration(
                              borderRadius: BorderRadius.circular(6),
                              color: BSTheme.accent.withValues(alpha: 0.12),
                              border: Border.all(
                                  color:
                                      BSTheme.accent.withValues(alpha: 0.3)),
                            ),
                            child: const Text(
                              'PORTABLE',
                              style: TextStyle(
                                fontFamily: 'Geist',
                                fontSize: 9,
                                fontWeight: FontWeight.w700,
                                letterSpacing: 1.2,
                                color: BSTheme.accent,
                              ),
                            ),
                          ),
                        ],
                      ],
                    ),
                    const SizedBox(height: 3),
                    _locationRow(),
                    const SizedBox(height: 8),
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 8, vertical: 3),
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
              Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Semantics(
                    label: label,
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 10, vertical: 5),
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(8),
                        color: color.withValues(alpha: 0.12),
                        border:
                            Border.all(color: color.withValues(alpha: 0.3)),
                      ),
                      child: Text(
                        label,
                        style: TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 10,
                          fontWeight: FontWeight.w700,
                          letterSpacing: 1.2,
                          color: color,
                        ),
                      ),
                    ),
                  ),
                  if (node.isOnVacation &&
                      node.vacationUntil.isNotEmpty) ...[
                    const SizedBox(height: 4),
                    Text(
                      'back ${_fmtDate(node.vacationUntil)}',
                      style: const TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 10,
                          color: BSTheme.warm),
                    ),
                  ],
                ],
              ),
            ],
          ),
          // Sleeping portable: Start tonight + Vacation
          if (node.portable && node.isSleeping) ...[
            const SizedBox(height: 14),
            Row(
              children: [
                Expanded(
                  child: FilledButton.icon(
                    onPressed: () => _showStartTonight(context),
                    icon:
                        const Icon(Icons.play_arrow_rounded, size: 18),
                    label: const Text('Start tonight'),
                  ),
                ),
                const SizedBox(width: 10),
                OutlinedButton.icon(
                  onPressed: () => _showVacation(context),
                  icon:
                      const Icon(Icons.event_busy_outlined, size: 16),
                  label: const Text('Vacation'),
                  style: OutlinedButton.styleFrom(
                    side: const BorderSide(color: BSTheme.glassBorder),
                    foregroundColor: BSTheme.ink2,
                  ),
                ),
              ],
            ),
          ],
          // Vacation: cancel
          if (node.isOnVacation) ...[
            const SizedBox(height: 8),
            TextButton(
              onPressed: () => _cancelVacation(context),
              style: TextButton.styleFrom(
                padding: EdgeInsets.zero,
                foregroundColor: BSTheme.warm,
              ),
              child: const Text('Cancel vacation',
                  style: TextStyle(fontSize: 13)),
            ),
          ],
          // Active portable: end session
          if (node.portable && node.status == 'active') ...[
            const SizedBox(height: 8),
            TextButton(
              onPressed: () => _endSession(context),
              style: TextButton.styleFrom(
                padding: EdgeInsets.zero,
                foregroundColor: BSTheme.ink3,
              ),
              child: const Text('End session',
                  style: TextStyle(fontSize: 13)),
            ),
          ],
        ],
      ),
    );
  }

  Widget _locationRow() {
    String loc;
    if (node.status == 'active' &&
        node.portable &&
        (node.sessionCity.isNotEmpty || node.sessionSiteName.isNotEmpty)) {
      final parts = [node.sessionSiteName, node.sessionCity]
          .where((p) => p.isNotEmpty)
          .toList();
      loc = parts.join(', ');
    } else {
      loc = node.location;
    }
    if (loc.isEmpty) return const SizedBox.shrink();
    return Row(
      children: [
        const Icon(Icons.location_on_outlined,
            size: 12, color: BSTheme.ink3),
        const SizedBox(width: 3),
        Flexible(
          child: Text(
            loc,
            style: const TextStyle(
                fontFamily: 'Geist', fontSize: 12, color: BSTheme.ink3),
            overflow: TextOverflow.ellipsis,
          ),
        ),
      ],
    );
  }

  String _fmtDate(String iso) {
    try {
      final d = DateTime.parse(iso);
      const m = [
        'Jan','Feb','Mar','Apr','May','Jun',
        'Jul','Aug','Sep','Oct','Nov','Dec'
      ];
      return '${m[d.month - 1]} ${d.day}';
    } catch (_) {
      return iso;
    }
  }

  Future<void> _showStartTonight(BuildContext context) async {
    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _StartTonightSheet(node: node),
    );
    if (ok == true) onRefresh();
  }

  Future<void> _showVacation(BuildContext context) async {
    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _VacationSheet(node: node),
    );
    if (ok == true) onRefresh();
  }

  Future<void> _cancelVacation(BuildContext context) async {
    try {
      await context.read<AppState>().api.cancelNodeVacation(node.nodeId);
      onRefresh();
    } catch (e) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Failed to cancel vacation: $e')),
        );
      }
    }
  }

  Future<void> _endSession(BuildContext context) async {
    try {
      await context.read<AppState>().api.endNodeSession(node.nodeId);
      onRefresh();
    } catch (e) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Failed to end session: $e')),
        );
      }
    }
  }
}

// ── Status dot ────────────────────────────────────────────────────────────────

class _StatusDot extends StatefulWidget {
  const _StatusDot({required this.status});
  final String status;

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
    final pulsing =
        widget.status == 'active' || widget.status == 'sleeping';
    _ctrl = AnimationController(
      vsync: this,
      duration: widget.status == 'sleeping'
          ? const Duration(milliseconds: 2600)
          : const Duration(milliseconds: 1400),
    );
    if (pulsing) _ctrl.repeat(reverse: true);
    _anim = Tween<double>(begin: 0.3, end: 1.0).animate(
      CurvedAnimation(parent: _ctrl, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  Color get _color {
    switch (widget.status) {
      case 'active':
        return BSTheme.success;
      case 'sleeping':
        return BSTheme.accent;
      case 'vacation':
        return BSTheme.warm;
      default:
        return BSTheme.danger;
    }
  }

  @override
  Widget build(BuildContext context) {
    final color = _color;
    final pulsing =
        widget.status == 'active' || widget.status == 'sleeping';
    return AnimatedBuilder(
      animation: _anim,
      builder: (_, __) => Container(
        width: 6,
        height: 6,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: color,
          boxShadow: pulsing
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

// ── Start Tonight sheet ───────────────────────────────────────────────────────

class _StartTonightSheet extends StatefulWidget {
  const _StartTonightSheet({required this.node});
  final Node node;

  @override
  State<_StartTonightSheet> createState() => _StartTonightSheetState();
}

enum _LocStep { idle, geocoding, confirming, confirmed }

class _StartTonightSheetState extends State<_StartTonightSheet> {
  final _locationCtrl = TextEditingController();
  final _siteCtrl = TextEditingController();
  double? _lat;
  double? _lon;
  String _city = '';
  String? _resolved;
  _LocStep _locStep = _LocStep.idle;
  bool _locating = false;
  bool _fetchingSky = false;
  Map<String, dynamic>? _sky;
  bool _busy = false;
  String? _error;
  bool _showPrev = true;

  @override
  void initState() {
    super.initState();
    _locationCtrl.addListener(() => setState(() {}));
    if (widget.node.previousLocations.isEmpty) _showPrev = false;
  }

  @override
  void dispose() {
    _locationCtrl.dispose();
    _siteCtrl.dispose();
    super.dispose();
  }

  void _pickPrev(PreviousLocation loc) {
    setState(() {
      _lat = loc.lat;
      _lon = loc.lon;
      _city = loc.city;
      _resolved = loc.label;
      _locationCtrl.text = loc.label;
      _siteCtrl.text = loc.siteName;
      _locStep = _LocStep.confirmed;
      _showPrev = false;
    });
    _fetchSky();
  }

  Future<void> _detectGps() async {
    setState(() { _locating = true; _error = null; });
    try {
      final pos = await html.window.navigator.geolocation
          .getCurrentPosition(enableHighAccuracy: true);
      final lat = (pos.coords!.latitude as num).toDouble();
      final lon = (pos.coords!.longitude as num).toDouble();
      if (!mounted) return;
      setState(() {
        _lat = lat;
        _lon = lon;
        _city = '';
        _locating = false;
        if (_locationCtrl.text.trim().isEmpty) {
          _locationCtrl.text =
              '${lat.toStringAsFixed(4)}°, ${lon.toStringAsFixed(4)}°';
        }
        _locStep = _LocStep.confirmed;
      });
      _fetchSky();
    } catch (_) {
      if (mounted) {
        setState(() {
          _locating = false;
          _error = 'Location access denied. Enter a location name instead.';
        });
      }
    }
  }

  Future<void> _lookup() async {
    final q = _locationCtrl.text.trim();
    if (q.isEmpty) return;
    setState(() { _locStep = _LocStep.geocoding; _error = null; });
    try {
      final uri = Uri.https('nominatim.openstreetmap.org', '/search', {
        'q': q, 'format': 'json', 'limit': '1', 'addressdetails': '1',
      });
      final resp = await http.get(
          uri, headers: {'User-Agent': 'BoundlessSkiesApp/1.0'});
      if (!mounted) return;
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as List;
        if (data.isNotEmpty) {
          final r = data.first as Map<String, dynamic>;
          final lat = double.tryParse(r['lat'] as String? ?? '');
          final lon = double.tryParse(r['lon'] as String? ?? '');
          if (lat != null && lon != null) {
            final addr = r['address'] as Map<String, dynamic>?;
            _city = addr?['city'] as String? ??
                addr?['town'] as String? ??
                addr?['village'] as String? ?? '';
            setState(() {
              _lat = lat;
              _lon = lon;
              _resolved = r['display_name'] as String?;
              _locStep = _LocStep.confirming;
            });
            return;
          }
        }
        setState(() {
          _locStep = _LocStep.idle;
          _error = 'No location found for "$q". Try a more specific name.';
        });
      } else {
        setState(() {
          _locStep = _LocStep.idle;
          _error = 'Location lookup failed. Try again or use the GPS button.';
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _locStep = _LocStep.idle;
          _error = 'Location lookup failed. Try again or use the GPS button.';
        });
      }
    }
  }

  Future<void> _fetchSky() async {
    if (_lat == null || _lon == null) return;
    setState(() { _fetchingSky = true; _sky = null; });
    try {
      final data =
          await context.read<AppState>().api.skyQuality(_lat!, _lon!);
      if (mounted) setState(() { _sky = data; _fetchingSky = false; });
    } catch (_) {
      if (mounted) setState(() => _fetchingSky = false);
    }
  }

  Future<void> _submit() async {
    if (_lat == null || _lon == null) return;
    setState(() { _busy = true; _error = null; });
    try {
      await context.read<AppState>().api.startNodeSession(
        widget.node.nodeId,
        lat: _lat!,
        lon: _lon!,
        city: _city.isNotEmpty ? _city : _locationCtrl.text.trim(),
        siteName: _siteCtrl.text.trim(),
      );
      if (mounted) Navigator.of(context).pop(true);
    } catch (e) {
      if (mounted) setState(() { _busy = false; _error = '$e'; });
    }
  }

  Color _bortleColor(int b) {
    if (b <= 2) return BSTheme.success;
    if (b <= 4) return BSTheme.accent;
    if (b <= 6) return BSTheme.warm;
    return BSTheme.danger;
  }

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    return Padding(
      padding: EdgeInsets.only(
        left: 24, right: 24, top: 28,
        bottom: MediaQuery.of(context).viewInsets.bottom + 28,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Center(
            child: Container(
              width: 36, height: 4,
              margin: const EdgeInsets.only(bottom: 20),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(2),
                color: BSTheme.glassBorder,
              ),
            ),
          ),
          Text("Start tonight's session", style: tt.headlineSmall),
          const SizedBox(height: 4),
          Text('Where are you observing tonight?', style: tt.bodyMedium),
          const SizedBox(height: 20),

          // Previous locations
          if (_showPrev && widget.node.previousLocations.isNotEmpty) ...[
            Text('Previous locations',
                style: tt.labelSmall?.copyWith(letterSpacing: 1.4)),
            const SizedBox(height: 10),
            ...widget.node.previousLocations
                .map((l) => _PrevTile(loc: l, onTap: () => _pickPrev(l))),
            const SizedBox(height: 4),
            TextButton.icon(
              onPressed: () => setState(() => _showPrev = false),
              icon: const Icon(Icons.add_location_outlined, size: 16),
              label: const Text('Different location'),
              style: TextButton.styleFrom(
                padding: EdgeInsets.zero,
                foregroundColor: BSTheme.ink3,
              ),
            ),
          ],

          // Location entry
          if (!_showPrev && _locStep != _LocStep.confirmed) ...[
            if (_locStep == _LocStep.geocoding) ...[
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
            ] else if (_locStep == _LocStep.confirming) ...[
              Text('Is this the right location?', style: tt.titleMedium),
              const SizedBox(height: 16),
              _LocConfirmCard(
                  display: _resolved ?? _locationCtrl.text,
                  lat: _lat!,
                  lon: _lon!),
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: () =>
                          setState(() => _locStep = _LocStep.idle),
                      icon: const Icon(Icons.edit_outlined, size: 16),
                      label: const Text('Edit'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: FilledButton.icon(
                      onPressed: () {
                        setState(() => _locStep = _LocStep.confirmed);
                        _fetchSky();
                      },
                      icon: const Icon(Icons.check, size: 16),
                      label: const Text('Yes, this is it'),
                    ),
                  ),
                ],
              ),
            ] else ...[
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(
                    child: TextField(
                      controller: _locationCtrl,
                      enabled: !_locating,
                      autofocus: widget.node.previousLocations.isEmpty,
                      decoration: const InputDecoration(
                        labelText: "Tonight's location",
                        hintText: 'e.g. Cherry Springs State Park',
                        border: OutlineInputBorder(),
                        prefixIcon: Icon(Icons.location_on_outlined),
                      ),
                      textInputAction: TextInputAction.search,
                      onSubmitted: (_) => _lookup(),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Tooltip(
                    message: 'Use device GPS',
                    child: FilledButton.tonal(
                      onPressed: _locating ? null : _detectGps,
                      style: FilledButton.styleFrom(
                        minimumSize: const Size(48, 56),
                        padding: EdgeInsets.zero,
                      ),
                      child: _locating
                          ? const SizedBox(
                              width: 18, height: 18,
                              child: CircularProgressIndicator(strokeWidth: 2))
                          : const Icon(Icons.my_location),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              FilledButton(
                onPressed:
                    _locationCtrl.text.trim().isEmpty ? null : _lookup,
                child: const Text('Confirm location'),
              ),
            ],
          ],

          // Confirmed location: sky quality + site name + scanner note
          if (_locStep == _LocStep.confirmed) ...[
            Row(
              children: [
                const Icon(Icons.check_circle_outline,
                    size: 16, color: BSTheme.success),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    _resolved ?? _locationCtrl.text,
                    style: const TextStyle(
                        fontFamily: 'Geist', fontSize: 13, color: BSTheme.ink),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                if (!_busy)
                  TextButton(
                    onPressed: () => setState(() {
                      _locStep = _LocStep.idle;
                      _sky = null;
                      _showPrev =
                          widget.node.previousLocations.isNotEmpty;
                    }),
                    style: TextButton.styleFrom(
                        padding:
                            const EdgeInsets.symmetric(horizontal: 8)),
                    child: const Text('Change'),
                  ),
              ],
            ),
            const SizedBox(height: 12),
            if (_fetchingSky)
              const Center(
                child: SizedBox(
                    width: 20, height: 20,
                    child: CircularProgressIndicator(strokeWidth: 2)),
              )
            else if (_sky != null)
              Row(
                children: [
                  _SkyPill(
                    label: 'MPSAS',
                    value: (_sky!['mpsas'] as num?)
                            ?.toStringAsFixed(1) ??
                        '--',
                    color: BSTheme.accent,
                  ),
                  const SizedBox(width: 8),
                  _SkyPill(
                    label: 'BORTLE',
                    value: '${_sky!['bortle'] ?? '--'}',
                    color: _bortleColor(
                        (_sky!['bortle'] as num?)?.toInt() ?? 5),
                  ),
                ],
              ),
            const SizedBox(height: 14),
            TextField(
              controller: _siteCtrl,
              decoration: const InputDecoration(
                labelText: 'Site name (optional)',
                hintText: 'e.g. Starfield Ranch, backyard',
                border: OutlineInputBorder(),
                prefixIcon: Icon(Icons.nature_outlined),
              ),
              textInputAction: TextInputAction.done,
            ),
            const SizedBox(height: 14),
            Container(
              padding: const EdgeInsets.symmetric(
                  horizontal: 14, vertical: 12),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(12),
                color: BSTheme.accent.withValues(alpha: 0.07),
                border: Border.all(
                    color: BSTheme.accent.withValues(alpha: 0.2)),
              ),
              child: Row(
                children: [
                  const Icon(Icons.radar_outlined,
                      size: 16, color: BSTheme.accent),
                  const SizedBox(width: 10),
                  const Expanded(
                    child: Text(
                      'A sky scan will run automatically when your telescope connects.',
                      style: TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 12,
                        color: BSTheme.ink3,
                        height: 1.4,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],

          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(_error!,
                style:
                    const TextStyle(color: BSTheme.danger, fontSize: 13)),
          ],

          const SizedBox(height: 20),

          if (_locStep == _LocStep.confirmed)
            _busy
                ? const Center(child: CircularProgressIndicator())
                : FilledButton.icon(
                    onPressed: _submit,
                    icon:
                        const Icon(Icons.play_arrow_rounded, size: 18),
                    label: const Text('Start observing'),
                  ),

          const SizedBox(height: 8),
        ],
      ),
    );
  }
}

class _PrevTile extends StatelessWidget {
  const _PrevTile({required this.loc, required this.onTap});
  final PreviousLocation loc;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        margin: const EdgeInsets.only(bottom: 6),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(12),
          color: BSTheme.glassBg,
          border: Border.all(color: BSTheme.glassBorder),
        ),
        child: Row(
          children: [
            const Icon(Icons.history_outlined,
                size: 16, color: BSTheme.ink3),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                loc.label,
                style: const TextStyle(
                    fontFamily: 'Geist', fontSize: 14, color: BSTheme.ink),
              ),
            ),
            const Icon(Icons.chevron_right, size: 18, color: BSTheme.ink3),
          ],
        ),
      ),
    );
  }
}

class _LocConfirmCard extends StatelessWidget {
  const _LocConfirmCard(
      {required this.display, required this.lat, required this.lon});
  final String display;
  final double lat;
  final double lon;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding:
          const EdgeInsets.symmetric(vertical: 14, horizontal: 16),
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
                child: Icon(Icons.location_on,
                    size: 16, color: BSTheme.accent),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  display,
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
              '${lat.toStringAsFixed(5)}°, ${lon.toStringAsFixed(5)}°',
              style: const TextStyle(
                  fontFamily: 'Geist', fontSize: 12, color: BSTheme.ink3),
            ),
          ),
        ],
      ),
    );
  }
}

class _SkyPill extends StatelessWidget {
  const _SkyPill(
      {required this.label, required this.value, required this.color});
  final String label;
  final String value;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding:
          const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(8),
        color: color.withValues(alpha: 0.10),
        border: Border.all(color: color.withValues(alpha: 0.25)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 9,
              fontWeight: FontWeight.w700,
              letterSpacing: 1.2,
              color: color,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            value,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 16,
              fontWeight: FontWeight.w600,
              color: color,
              letterSpacing: -0.3,
            ),
          ),
        ],
      ),
    );
  }
}

// ── Vacation sheet ────────────────────────────────────────────────────────────

class _VacationSheet extends StatefulWidget {
  const _VacationSheet({required this.node});
  final Node node;

  @override
  State<_VacationSheet> createState() => _VacationSheetState();
}

class _VacationSheetState extends State<_VacationSheet> {
  final _ctrl = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _ctrl.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  int? get _nights {
    final v = int.tryParse(_ctrl.text.trim());
    return (v != null && v > 0) ? v : null;
  }

  String? get _backLabel {
    final n = _nights;
    if (n == null) return null;
    final d = DateTime.now().add(Duration(days: n));
    const m = [
      'Jan','Feb','Mar','Apr','May','Jun',
      'Jul','Aug','Sep','Oct','Nov','Dec'
    ];
    return '${m[d.month - 1]} ${d.day}';
  }

  String? get _untilIso {
    final n = _nights;
    if (n == null) return null;
    final d = DateTime.now().add(Duration(days: n));
    return '${d.year}-'
        '${d.month.toString().padLeft(2, '0')}-'
        '${d.day.toString().padLeft(2, '0')}';
  }

  Future<void> _submit() async {
    final until = _untilIso;
    if (until == null) return;
    setState(() { _busy = true; _error = null; });
    try {
      await context
          .read<AppState>()
          .api
          .setNodeVacation(widget.node.nodeId, until);
      if (mounted) Navigator.of(context).pop(true);
    } catch (e) {
      if (mounted) setState(() { _busy = false; _error = '$e'; });
    }
  }

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final bd = _backLabel;
    final canSubmit = _nights != null && !_busy;

    return Padding(
      padding: EdgeInsets.only(
        left: 24, right: 24, top: 28,
        bottom: MediaQuery.of(context).viewInsets.bottom + 28,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Center(
            child: Container(
              width: 36, height: 4,
              margin: const EdgeInsets.only(bottom: 20),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(2),
                color: BSTheme.glassBorder,
              ),
            ),
          ),
          Text('Set vacation', style: tt.headlineSmall),
          const SizedBox(height: 8),
          Text('How many nights will your telescope be offline?',
              style: tt.bodyMedium),
          const SizedBox(height: 24),
          TextField(
            controller: _ctrl,
            autofocus: true,
            keyboardType: TextInputType.number,
            inputFormatters: [FilteringTextInputFormatter.digitsOnly],
            decoration: const InputDecoration(
              labelText: 'Number of nights',
              hintText: 'e.g. 3',
              border: OutlineInputBorder(),
              suffixText: 'nights',
            ),
            textInputAction: TextInputAction.done,
            onSubmitted: (_) { if (canSubmit) _submit(); },
          ),
          if (bd != null) ...[
            const SizedBox(height: 14),
            Container(
              padding: const EdgeInsets.symmetric(
                  horizontal: 14, vertical: 12),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(12),
                color: BSTheme.warm.withValues(alpha: 0.08),
                border: Border.all(
                    color: BSTheme.warm.withValues(alpha: 0.25)),
              ),
              child: Row(
                children: [
                  const Icon(Icons.event_outlined,
                      size: 16, color: BSTheme.warm),
                  const SizedBox(width: 10),
                  Text(
                    'Back by $bd',
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 14,
                      fontWeight: FontWeight.w500,
                      color: BSTheme.warm,
                    ),
                  ),
                ],
              ),
            ),
          ],
          const SizedBox(height: 14),
          Text(
            'Your reliability score is paused during vacation. '
            "Missed nights won't count against your stats.",
            style: tt.bodySmall,
          ),
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(_error!,
                style:
                    const TextStyle(color: BSTheme.danger, fontSize: 13)),
          ],
          const SizedBox(height: 24),
          if (_busy)
            const Center(child: CircularProgressIndicator())
          else
            FilledButton.icon(
              onPressed: canSubmit ? _submit : null,
              icon: const Icon(Icons.event_busy_outlined, size: 18),
              label: const Text('Pause telescope'),
            ),
          const SizedBox(height: 8),
        ],
      ),
    );
  }
}

// ── Claim sheet (connect a new telescope) ─────────────────────────────────────

class _ClaimSheet extends StatefulWidget {
  const _ClaimSheet();

  @override
  State<_ClaimSheet> createState() => _ClaimSheetState();
}

enum _ScopeStep { idle, confirming, confirmed }

class _ClaimSheetState extends State<_ClaimSheet> {
  final _locationCtrl = TextEditingController();
  final _scopeCtrl = TextEditingController();
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

  // Telescope
  List<TelescopeSpec> _catalog = [];
  TelescopeSpec? _selectedScope;
  bool _scopeIsCustom = false;
  _ScopeStep _scopeStep = _ScopeStep.idle;

  // Portable
  bool? _portable; // null = not yet chosen

  @override
  void initState() {
    super.initState();
    _locationCtrl.addListener(() => setState(() {}));
    _scopeCtrl.addListener(() => setState(() {}));
    _loadCatalog();
  }

  Future<void> _loadCatalog() async {
    try {
      final list = await context.read<AppState>().api.telescopes();
      if (mounted) setState(() => _catalog = list);
    } catch (_) {}
  }

  List<TelescopeSpec> get _scopeSuggestions {
    final q = _scopeCtrl.text.trim().toLowerCase();
    if (q.isEmpty) return const [];
    return _catalog
        .where((s) =>
            !s.isCustom &&
            (s.displayName.toLowerCase().contains(q) ||
                s.cameraModel.toLowerCase().contains(q) ||
                s.key.replaceAll('_', ' ').contains(q)))
        .take(6)
        .toList();
  }

  @override
  void dispose() {
    _locationCtrl.dispose();
    _scopeCtrl.dispose();
    _pairCtrl.dispose();
    super.dispose();
  }

  void _resetScope() {
    _selectedScope = null;
    _scopeIsCustom = false;
    _scopeStep = _ScopeStep.idle;
    _portable = null;
    _lat = null;
    _lon = null;
    _resolvedLocation = null;
    _step = _LocStep.idle;
    _error = null;
  }

  void _selectScope(TelescopeSpec spec) {
    setState(() {
      _selectedScope = spec;
      _scopeIsCustom = false;
      _scopeCtrl.text = spec.displayName;
      _scopeStep = _ScopeStep.confirming;
      _error = null;
    });
  }

  void _useCustomScope() {
    setState(() {
      _selectedScope = null;
      _scopeIsCustom = true;
      _scopeStep = _ScopeStep.confirmed;
      _error = null;
    });
  }

  void _resetLocation() {
    _lat = null;
    _lon = null;
    _resolvedLocation = null;
    _step = _LocStep.idle;
    _error = null;
  }

  Future<void> _tryPortableGps() async {
    setState(() => _locating = true);
    try {
      final pos = await html.window.navigator.geolocation
          .getCurrentPosition(enableHighAccuracy: false);
      final lat = (pos.coords!.latitude as num).toDouble();
      final lon = (pos.coords!.longitude as num).toDouble();
      if (mounted) {
        setState(() {
          _lat = lat;
          _lon = lon;
          _locating = false;
          _locationCtrl.text =
              '${lat.toStringAsFixed(3)}°, ${lon.toStringAsFixed(3)}°';
          _step = _LocStep.confirmed;
        });
      }
    } catch (_) {
      if (mounted) setState(() => _locating = false);
    }
  }

  Future<void> _detectLocation() async {
    setState(() { _locating = true; _error = null; });
    try {
      final pos = await html.window.navigator.geolocation
          .getCurrentPosition(enableHighAccuracy: true);
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
    } catch (_) {
      if (mounted) {
        setState(() {
          _locating = false;
          _error =
              'Location access denied. Enter a location name instead.';
        });
      }
    }
  }

  Future<void> _lookupLocation() async {
    final query = _locationCtrl.text.trim();
    if (query.isEmpty) return;
    setState(() { _step = _LocStep.geocoding; _error = null; });
    try {
      final uri = Uri.https('nominatim.openstreetmap.org', '/search', {
        'q': query, 'format': 'json', 'limit': '1', 'addressdetails': '1',
      });
      final resp = await http.get(
          uri, headers: {'User-Agent': 'BoundlessSkiesApp/1.0'});
      if (!mounted) return;
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as List;
        if (data.isNotEmpty) {
          final r = data.first as Map<String, dynamic>;
          final lat = double.tryParse(r['lat'] as String? ?? '');
          final lon = double.tryParse(r['lon'] as String? ?? '');
          if (lat != null && lon != null) {
            setState(() {
              _lat = lat;
              _lon = lon;
              _resolvedLocation = r['display_name'] as String?;
              _step = _LocStep.confirming;
            });
            return;
          }
        }
        setState(() {
          _step = _LocStep.idle;
          _error =
              'No location found for "$query". Try a more specific name.';
        });
      } else {
        setState(() {
          _step = _LocStep.idle;
          _error =
              'Location lookup failed. Try again or use the GPS button.';
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _step = _LocStep.idle;
          _error =
              'Location lookup failed. Try again or use the GPS button.';
        });
      }
    }
  }

  Future<void> _generate() async {
    final location = _locationCtrl.text.trim();
    setState(() { _busy = true; _error = null; _code = null; });
    try {
      final scopeModel =
          _scopeIsCustom ? _scopeCtrl.text.trim() : _selectedScope?.displayName;
      final code = await context.read<AppState>().api.generateActivationCode(
            locationName: location.isEmpty ? null : location,
            lat: _lat,
            lon: _lon,
            telescopeModel: scopeModel,
            telescopeSpecs: _selectedScope?.toSpecPayload(),
            portable: _portable ?? false,
          );
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
        left: 24, right: 24, top: 28,
        bottom: MediaQuery.of(context).viewInsets.bottom + 28,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Center(
            child: Container(
              width: 36, height: 4,
              margin: const EdgeInsets.only(bottom: 20),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(2),
                color: BSTheme.glassBorder,
              ),
            ),
          ),
          Text('Connect a telescope', style: tt.headlineSmall),
          const SizedBox(height: 10),
          if (_code == null) ...[
            ..._buildScopeSection(tt),
            if (_scopeStep == _ScopeStep.confirmed) ...[
              const SizedBox(height: 20),
              const Divider(height: 1),
              const SizedBox(height: 20),
              ..._buildPortableSection(tt),
            ],
            if (_scopeStep == _ScopeStep.confirmed && _portable != null) ...[
              const SizedBox(height: 20),
              const Divider(height: 1),
              const SizedBox(height: 20),
              ..._buildLocationSection(tt),
            ],
          ],
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(_error!,
                style:
                    const TextStyle(color: BSTheme.danger, fontSize: 13)),
          ],
          const SizedBox(height: 16),
          if (_code != null)
            ..._buildCodeSection(tt, context)
          else if (_busy)
            const Center(child: CircularProgressIndicator())
          else if (_scopeStep == _ScopeStep.confirmed &&
              _portable != null &&
              _step == _LocStep.confirmed)
            FilledButton(
              onPressed: _generate,
              child: const Text('Get activation code'),
            ),
          const SizedBox(height: 8),
        ],
      ),
    );
  }

  List<Widget> _buildPortableSection(TextTheme tt) {
    if (_portable != null) {
      return [
        Row(
          children: [
            Icon(
              _portable! ? Icons.backpack_outlined : Icons.home_outlined,
              size: 16,
              color: BSTheme.success,
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                _portable!
                    ? 'Portable — moves between sites'
                    : 'Fixed — stays in one place',
                style: const TextStyle(
                    fontFamily: 'Geist', fontSize: 13, color: BSTheme.ink),
              ),
            ),
            if (!_busy)
              TextButton(
                onPressed: () => setState(() {
                  _portable = null;
                  _step = _LocStep.idle;
                  _lat = null;
                  _lon = null;
                  _resolvedLocation = null;
                }),
                style: TextButton.styleFrom(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 8)),
                child: const Text('Change'),
              ),
          ],
        ),
      ];
    }

    if (_locating) {
      return [
        const Center(child: CircularProgressIndicator()),
        const SizedBox(height: 8),
        Center(
            child: Text('Detecting location…', style: tt.bodySmall)),
      ];
    }

    return [
      Text('Where does this telescope live?', style: tt.titleMedium),
      const SizedBox(height: 16),
      _PortableChoiceTile(
        icon: Icons.home_outlined,
        title: 'Fixed — stays in one place',
        subtitle: 'Observatory, rooftop, or permanent backyard setup.',
        onTap: () => setState(() => _portable = false),
      ),
      const SizedBox(height: 10),
      _PortableChoiceTile(
        icon: Icons.backpack_outlined,
        title: 'Portable — I move it to different sites',
        subtitle: 'Star parties, dark sky reserves, travelling.',
        onTap: () async {
          setState(() => _portable = true);
          await _tryPortableGps();
        },
      ),
    ];
  }

  List<Widget> _buildScopeSection(TextTheme tt) {
    switch (_scopeStep) {
      case _ScopeStep.idle:
        final suggestions = _scopeSuggestions;
        final typed = _scopeCtrl.text.trim();
        return [
          Text(
            "Which telescope is this? We'll fill in its specs for you.",
            style: tt.bodyMedium,
          ),
          const SizedBox(height: 20),
          TextField(
            controller: _scopeCtrl,
            autofocus: true,
            decoration: const InputDecoration(
              labelText: 'Telescope model',
              hintText: 'e.g. Seestar S50, Vespera II, Dwarf 3',
              border: OutlineInputBorder(),
              prefixIcon: Icon(Icons.travel_explore_outlined),
            ),
            textInputAction: TextInputAction.search,
          ),
          if (suggestions.isNotEmpty) ...[
            const SizedBox(height: 8),
            Container(
              decoration: BoxDecoration(
                border: Border.all(color: BSTheme.glassBorder),
                borderRadius: BorderRadius.circular(10),
              ),
              child: Column(
                children: [
                  for (final s in suggestions)
                    ListTile(
                      dense: true,
                      leading: const Icon(
                          Icons.center_focus_strong_outlined,
                          size: 18),
                      title: Text(s.displayName,
                          style: const TextStyle(fontSize: 14)),
                      subtitle: Text(
                        '${s.apertureMm.toStringAsFixed(0)} mm  ·  '
                        'f/${s.focalRatio.toStringAsFixed(1)}  ·  '
                        '${s.pixelScaleArcsec.toStringAsFixed(2)}″/px',
                        style: const TextStyle(
                            fontSize: 12, color: BSTheme.ink3),
                      ),
                      onTap: () => _selectScope(s),
                    ),
                ],
              ),
            ),
          ],
          const SizedBox(height: 12),
          if (typed.isNotEmpty && suggestions.isEmpty)
            OutlinedButton.icon(
              onPressed: _useCustomScope,
              icon: const Icon(Icons.tune, size: 16),
              label: Text('Use "$typed" — detect specs on connect'),
            ),
        ];

      case _ScopeStep.confirming:
        final s = _selectedScope!;
        return [
          Text('Is this your telescope?', style: tt.titleMedium),
          const SizedBox(height: 16),
          Container(
            padding: const EdgeInsets.symmetric(
                vertical: 14, horizontal: 16),
            decoration: BoxDecoration(
              border: Border.all(color: BSTheme.glassBorder),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    const Icon(Icons.center_focus_strong_outlined,
                        size: 18, color: BSTheme.accent),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        s.displayName,
                        style: const TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 15,
                          fontWeight: FontWeight.w600,
                          color: BSTheme.ink,
                        ),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 10),
                _specRow('Aperture',
                    '${s.apertureMm.toStringAsFixed(0)} mm'),
                _specRow(
                    'Focal length',
                    '${s.focalLengthMm.toStringAsFixed(0)} mm  '
                        '(f/${s.focalRatio.toStringAsFixed(1)})'),
                _specRow('Pixel scale',
                    '${s.pixelScaleArcsec.toStringAsFixed(2)}″/px'),
                _specRow('Field of view',
                    '${s.fovDeg.toStringAsFixed(2)}°'),
                _specRow(
                    'Mount',
                    s.mountType == 'alt_az'
                        ? 'Alt-azimuth'
                        : 'Equatorial'),
              ],
            ),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: () => setState(_resetScope),
                  icon: const Icon(Icons.edit_outlined, size: 16),
                  label: const Text('Change'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: FilledButton.icon(
                  onPressed: () => setState(
                      () => _scopeStep = _ScopeStep.confirmed),
                  icon: const Icon(Icons.check, size: 16),
                  label: const Text('Yes, this is it'),
                ),
              ),
            ],
          ),
        ];

      case _ScopeStep.confirmed:
        final label = _scopeIsCustom
            ? '${_scopeCtrl.text.trim()} (specs auto-detected on connect)'
            : _selectedScope!.displayName;
        return [
          Row(
            children: [
              const Icon(Icons.check_circle_outline,
                  size: 18, color: Colors.green),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  label,
                  style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 13,
                      color: BSTheme.ink),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (!_busy)
                TextButton(
                  onPressed: () => setState(_resetScope),
                  style: TextButton.styleFrom(
                      padding:
                          const EdgeInsets.symmetric(horizontal: 8)),
                  child: const Text('Change'),
                ),
            ],
          ),
        ];
    }
  }

  Widget _specRow(String label, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(
          children: [
            SizedBox(
              width: 96,
              child: Text(label,
                  style: const TextStyle(
                      fontSize: 12, color: BSTheme.ink3)),
            ),
            Expanded(
              child: Text(value,
                  style: const TextStyle(
                    fontSize: 13,
                    fontFamily: 'Geist',
                    fontWeight: FontWeight.w500,
                    color: BSTheme.ink,
                  )),
            ),
          ],
        ),
      );

  List<Widget> _buildLocationSection(TextTheme tt) {
    final isPortable = _portable == true;
    switch (_step) {
      case _LocStep.idle:
        return [
          Text(
            isPortable
                ? 'Enter a rough location to get started. '
                    "You'll set tonight's exact site each session."
                : 'Enter the location of the telescope — used for '
                    'scheduling and sky conditions.',
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
                  decoration: InputDecoration(
                    labelText: isPortable ? 'Home base' : 'Location',
                    hintText: 'e.g. Larchmont, NY or Dark Sky Ranch, TX',
                    border: const OutlineInputBorder(),
                    prefixIcon:
                        const Icon(Icons.location_on_outlined),
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
                          width: 18, height: 18,
                          child: CircularProgressIndicator(
                              strokeWidth: 2))
                      : const Icon(Icons.my_location),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          FilledButton(
            onPressed:
                _locationCtrl.text.trim().isEmpty ? null : _lookupLocation,
            child: const Text('Confirm location'),
          ),
        ];

      case _LocStep.geocoding:
        return [
          Text(
            isPortable
                ? 'Enter a rough location to get started.'
                : 'Enter the location of the telescope — used for '
                    'scheduling and sky conditions.',
            style: tt.bodyMedium,
          ),
          const SizedBox(height: 20),
          TextField(
            controller: _locationCtrl,
            enabled: false,
            decoration: InputDecoration(
              labelText: isPortable ? 'Home base' : 'Location',
              border: const OutlineInputBorder(),
              prefixIcon: const Icon(Icons.location_on_outlined),
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
          _LocConfirmCard(
            display: _resolvedLocation ?? _locationCtrl.text,
            lat: _lat!,
            lon: _lon!,
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
                  onPressed: () =>
                      setState(() => _step = _LocStep.confirmed),
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
              const Icon(Icons.check_circle_outline,
                  size: 18, color: Colors.green),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  '$label$coordStr',
                  style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 13,
                      color: BSTheme.ink),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (!_busy)
                TextButton(
                  onPressed: () => setState(_resetLocation),
                  style: TextButton.styleFrom(
                      padding:
                          const EdgeInsets.symmetric(horizontal: 8)),
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
      setState(() =>
          _error = 'Enter the pairing token shown in the terminal.');
      return;
    }
    setState(() { _pushing = true; _error = null; });
    try {
      await context.read<AppState>().api.pushActivationCode(
          token, _code!);
      if (mounted) setState(() { _pushed = true; _pushing = false; });
    } catch (e) {
      if (mounted)
        setState(() { _pushing = false; _error = '$e'; });
    }
  }

  List<Widget> _buildCodeSection(TextTheme tt, BuildContext context) {
    if (_pushed) {
      return [
        const Icon(Icons.check_circle_outline,
            color: Colors.green, size: 48),
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
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text('Done'),
        ),
      ];
    }

    return [
      Text('Enter the pairing token', style: tt.titleMedium),
      const SizedBox(height: 8),
      Text(
        'Look at the terminal window where the node software is running. '
        "You'll see a pairing token like NOVA-4827.",
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
          onPressed: _pairCtrl.text.trim().isEmpty
              ? null
              : _pushToTelescope,
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
          _resetScope();
          _scopeCtrl.clear();
        }),
        icon: const Icon(Icons.arrow_back, size: 16),
        label: const Text('Start over'),
      ),
    ];
  }
}

// ── Shared small widgets ──────────────────────────────────────────────────────

class _PortableChoiceTile extends StatelessWidget {
  const _PortableChoiceTile({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.onTap,
  });
  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(
            horizontal: 16, vertical: 14),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(12),
          color: BSTheme.glassBg,
          border: Border.all(color: BSTheme.glassBorder),
        ),
        child: Row(
          children: [
            Icon(icon, size: 22, color: BSTheme.accent),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 14,
                      fontWeight: FontWeight.w500,
                      color: BSTheme.ink,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: const TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 12,
                        color: BSTheme.ink3),
                  ),
                ],
              ),
            ),
            const Icon(Icons.chevron_right, size: 18, color: BSTheme.ink3),
          ],
        ),
      ),
    );
  }
}
