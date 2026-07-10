import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';

/// Open Aperture: "your telescope may have found something new" — the feed
/// of discovery candidates a member's nodes (or contributed frames) touched,
/// from the network's full-frame survey and archive re-processing.
class DiscoveriesScreen extends StatefulWidget {
  const DiscoveriesScreen({super.key});

  @override
  State<DiscoveriesScreen> createState() => _DiscoveriesScreenState();
}

class _DiscoveriesScreenState extends State<DiscoveriesScreen> {
  late Future<List<Discovery>> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<List<Discovery>> _load() {
    final state = context.read<AppState>();
    return state.api.discoveries().catchError((Object e) {
      state.handleAuthError(e);
      throw e;
    });
  }

  Future<void> _refresh() async {
    setState(() => _future = _load());
    await _future;
  }

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.of(context).padding.top;
    final bottom = MediaQuery.of(context).padding.bottom;

    return Scaffold(
      backgroundColor: BSTheme.night,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        elevation: 0,
        title: const Text(
          'Open Aperture',
          style: TextStyle(
            fontFamily: 'Geist',
            fontSize: 16,
            fontWeight: FontWeight.w800,
            color: BSTheme.ink,
          ),
        ),
        iconTheme: const IconThemeData(color: BSTheme.ink2),
      ),
      body: AsyncView<List<Discovery>>(
        future: _future,
        onRefresh: _refresh,
        isEmpty: (list) => list.isEmpty,
        emptyMessage: 'No discoveries yet — every frame your nodes capture '
            'is scanned for the unexpected.',
        builder: (context, discoveries) => ListView.builder(
          padding: EdgeInsets.fromLTRB(16, top + 8, 16, bottom + 24),
          itemCount: discoveries.length + 1,
          itemBuilder: (context, i) {
            if (i == 0) {
              return const Padding(
                padding: EdgeInsets.only(bottom: 16),
                child: Text(
                  'Every frame your telescope captures is measured against '
                  'the network\'s full history. When something deviates — a '
                  'new variable, an outburst, an unknown transient — it '
                  'shows up here.',
                  style: TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    color: BSTheme.ink2,
                    height: 1.4,
                  ),
                ),
              );
            }
            return _DiscoveryCard(discovery: discoveries[i - 1]);
          },
        ),
      ),
    );
  }
}

(Color, String) _stateStyle(String state) {
  switch (state) {
    case 'confirmed':
      return (BSTheme.success, 'Confirmed');
    case 'known_vsx':
      return (BSTheme.accent, 'Known variable');
    case 'known_tns':
      return (BSTheme.accent, 'Known transient');
    case 'candidate':
      return (BSTheme.warm, 'Candidate');
    case 'crossmatching':
      return (BSTheme.ink3, 'Checking catalogs');
    case 'detected':
      return (BSTheme.warm, 'Detected');
    case 'rejected':
      return (BSTheme.ink3, 'Ruled out');
    default:
      return (BSTheme.ink3, state);
  }
}

class _DiscoveryCard extends StatelessWidget {
  const _DiscoveryCard({required this.discovery});
  final Discovery discovery;

  @override
  Widget build(BuildContext context) {
    final (color, label) = _stateStyle(discovery.state);
    final name = discovery.tnsName.isNotEmpty
        ? discovery.tnsName
        : discovery.vsxName.isNotEmpty
            ? discovery.vsxName
            : discovery.sourceKey;

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: BSTheme.surface.withValues(alpha: 0.86),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: BSTheme.glassBorder),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 36,
                height: 36,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: color.withValues(alpha: 0.14),
                  border: Border.all(color: color.withValues(alpha: 0.35)),
                ),
                child: Icon(
                  discovery.isKnown ? Icons.verified_outlined : Icons.auto_awesome,
                  size: 18,
                  color: color,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      name,
                      style: const TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 14,
                        fontWeight: FontWeight.w800,
                        color: BSTheme.ink,
                      ),
                    ),
                    if (discovery.kind.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 2),
                        child: Text(
                          discovery.kind,
                          style: const TextStyle(
                            fontFamily: 'Geist',
                            fontSize: 12,
                            color: BSTheme.ink2,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                decoration: BoxDecoration(
                  color: color.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(6),
                  border: Border.all(color: color.withValues(alpha: 0.3)),
                ),
                child: Text(
                  label,
                  style: TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    color: color,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              _Datum(
                label: 'Δ MAG',
                value: discovery.magnitudeDelta.toStringAsFixed(2),
              ),
              _Datum(
                label: discovery.retrospective ? 'FROM' : 'NODES',
                value: discovery.retrospective
                    ? 'archive'
                    : '${discovery.nNodes}',
              ),
              _Datum(
                label: 'FILTER',
                value: discovery.filter.isEmpty ? '—' : discovery.filter,
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _Datum extends StatelessWidget {
  const _Datum({required this.label, required this.value});
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 9,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.6,
              color: BSTheme.ink3,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            value,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 13,
              fontWeight: FontWeight.w700,
              color: BSTheme.ink,
            ),
          ),
        ],
      ),
    );
  }
}
