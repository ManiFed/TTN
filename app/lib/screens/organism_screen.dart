import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';

/// THE ORGANISM: what the network just did on its own, live.
///
/// Shows every node's current second-scale phase plus the network's
/// self-healing activity in the last 24h — mid-night reflows (a
/// clouded-out node's targets moved to a dark node) and reflex
/// confirmations (a just-promoted discovery candidate auto-confirmed on
/// other dark nodes within seconds). Public feed, no auth required.
class OrganismScreen extends StatefulWidget {
  const OrganismScreen({super.key});

  @override
  State<OrganismScreen> createState() => _OrganismScreenState();
}

class _OrganismScreenState extends State<OrganismScreen> {
  late Future<OrganismFeed> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<OrganismFeed> _load() {
    final state = context.read<AppState>();
    return state.api.organism().catchError((Object e) {
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
          'The Organism',
          style: TextStyle(
            fontFamily: 'Geist',
            fontSize: 16,
            fontWeight: FontWeight.w800,
            color: BSTheme.ink,
          ),
        ),
        iconTheme: const IconThemeData(color: BSTheme.ink2),
      ),
      body: AsyncView<OrganismFeed>(
        future: _future,
        onRefresh: _refresh,
        isEmpty: (feed) => feed.fleet.isEmpty,
        emptyMessage: 'No nodes reporting live state yet.',
        builder: (context, feed) => ListView(
          padding: EdgeInsets.fromLTRB(16, top + 8, 16, bottom + 24),
          children: [
            const Text(
              'Right now, the network reacts on its own — clouded-out nodes '
              'hand their targets to dark ones, and new discoveries get '
              'confirmed automatically within seconds.',
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                color: BSTheme.ink2,
                height: 1.4,
              ),
            ),
            const SizedBox(height: 20),
            _SectionLabel('FLEET · ${feed.fleet.length} NODE'
                '${feed.fleet.length == 1 ? '' : 'S'}'),
            const SizedBox(height: 8),
            ...feed.fleet.map((n) => _FleetTile(node: n)),
            const SizedBox(height: 20),
            const _SectionLabel('REFLOWS · LAST 24H'),
            const SizedBox(height: 8),
            if (feed.reflows.isEmpty)
              const _EmptyRow('No dropouts tonight — nothing needed reflowing.')
            else
              ...feed.reflows.map((r) => _ReflowTile(event: r)),
            const SizedBox(height: 20),
            const _SectionLabel('REFLEX CONFIRMATIONS · LAST 24H'),
            const SizedBox(height: 8),
            if (feed.reflexConfirmations.isEmpty)
              const _EmptyRow('No candidates auto-confirmed tonight.')
            else
              ...feed.reflexConfirmations.map((r) => _ReflexTile(event: r)),
          ],
        ),
      ),
    );
  }
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Text(
        text,
        style: const TextStyle(
          fontFamily: 'Geist',
          fontSize: 11,
          fontWeight: FontWeight.w600,
          letterSpacing: 1.0,
          color: BSTheme.ink3,
        ),
      );
}

class _EmptyRow extends StatelessWidget {
  const _EmptyRow(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
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

(Color, IconData, String) _phaseStyle(String phase) {
  switch (phase) {
    case 'exposing':
      return (BSTheme.success, Icons.camera_outlined, 'Exposing');
    case 'slewing':
      return (BSTheme.accent, Icons.sync, 'Slewing');
    case 'stacking':
      return (BSTheme.accent, Icons.layers_outlined, 'Stacking');
    case 'clouded':
      return (BSTheme.warm, Icons.cloud_outlined, 'Clouded');
    case 'parked':
      return (BSTheme.ink3, Icons.pause_circle_outline, 'Parked');
    case 'offline':
      return (BSTheme.danger, Icons.wifi_off, 'Offline');
    case 'daylight':
      return (BSTheme.ink3, Icons.wb_sunny_outlined, 'Daylight');
    default:
      return (BSTheme.ink3, Icons.brightness_2_outlined, 'Idle');
  }
}

class _FleetTile extends StatelessWidget {
  const _FleetTile({required this.node});
  final FleetNode node;

  @override
  Widget build(BuildContext context) {
    final (color, icon, label) = _phaseStyle(node.phase);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BSTheme.surface.withValues(alpha: 0.86),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: BSTheme.glassBorder),
      ),
      child: Row(
        children: [
          Container(
            width: 34,
            height: 34,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: color.withValues(alpha: 0.14),
              border: Border.all(color: color.withValues(alpha: 0.35)),
            ),
            child: Icon(icon, size: 17, color: color),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  node.nodeId,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                    color: BSTheme.ink,
                  ),
                ),
                if (node.targetName.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      node.targetName,
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
          if (!node.online)
            const Padding(
              padding: EdgeInsets.only(right: 8),
              child: Icon(Icons.wifi_off, size: 14, color: BSTheme.danger),
            ),
          Text(
            label,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: color,
            ),
          ),
        ],
      ),
    );
  }
}

class _ReflowTile extends StatelessWidget {
  const _ReflowTile({required this.event});
  final ReflowEvent event;

  Color get _outcomeColor {
    switch (event.outcome) {
      case 'delivered':
        return BSTheme.success;
      case 'missed':
        return BSTheme.danger;
      default:
        return BSTheme.accent;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BSTheme.surface.withValues(alpha: 0.86),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: BSTheme.glassBorder),
      ),
      child: Row(
        children: [
          Icon(Icons.swap_horiz, size: 18, color: _outcomeColor),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  '${event.fromNode} → ${event.toNode}',
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                    color: BSTheme.ink,
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(
                    event.targetName.isEmpty ? '—' : event.targetName,
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
          Text(
            event.outcome,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: _outcomeColor,
            ),
          ),
        ],
      ),
    );
  }
}

class _ReflexTile extends StatelessWidget {
  const _ReflexTile({required this.event});
  final ReflexConfirmation event;

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BSTheme.surface.withValues(alpha: 0.86),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: BSTheme.glassBorder),
      ),
      child: Row(
        children: [
          const Icon(Icons.bolt, size: 18, color: BSTheme.success),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  event.name,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                    color: BSTheme.ink,
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(
                    'Confirming on ${event.nodeIds.length} node'
                    '${event.nodeIds.length == 1 ? '' : 's'}',
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
        ],
      ),
    );
  }
}
