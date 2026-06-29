import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';
import 'target_detail_screen.dart';

/// Recent photometric measurements and per-night summaries.
class ObservationsTab extends StatefulWidget {
  const ObservationsTab({super.key});

  @override
  State<ObservationsTab> createState() => _ObservationsTabState();
}

class _Data {
  const _Data({required this.nights, required this.observations});
  final List<NightSummary> nights;
  final List<Observation> observations;
}

class _ObservationsTabState extends State<ObservationsTab> {
  late Future<_Data> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<_Data> _load() async {
    final state = context.read<AppState>();
    final nightsFuture =
        state.api.nights(limit: 30).catchError((_) => <NightSummary>[]);
    final obsFuture =
        state.api.observations(days: 365, limit: 100000).catchError((e) {
      state.handleAuthError(e);
      throw e;
    });
    return _Data(nights: await nightsFuture, observations: await obsFuture);
  }

  Future<void> _refresh() async => setState(() => _future = _load());

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.of(context).padding.top + kToolbarHeight;
    final bottom = MediaQuery.of(context).padding.bottom + 64;

    return AsyncView<_Data>(
      future: _future,
      onRefresh: _refresh,
      isEmpty: (d) => d.observations.isEmpty && d.nights.isEmpty,
      emptyMessage: 'No observations yet.\nThey appear here after a clear night.',
      builder: (context, data) => CustomScrollView(
        slivers: [
          SliverToBoxAdapter(child: SizedBox(height: top + 8)),
          SliverToBoxAdapter(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'OBSERVATION HISTORY',
                    style: TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                      letterSpacing: 1.0,
                      color: BSTheme.ink3,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    '${data.observations.length} measurements · last 365 days',
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 13,
                      color: BSTheme.ink2,
                    ),
                  ),
                ],
              ),
            ),
          ),
          if (data.nights.isNotEmpty)
            SliverToBoxAdapter(child: _NightsStrip(nights: data.nights)),
          SliverPadding(
            padding: EdgeInsets.fromLTRB(16, 8, 16, bottom + 16),
            sliver: data.observations.isEmpty
                ? SliverToBoxAdapter(
                    child: Center(
                      child: Padding(
                        padding: const EdgeInsets.only(top: 32),
                        child: Text(
                          'No individual measurements in the last year.',
                          textAlign: TextAlign.center,
                          style: const TextStyle(
                              color: BSTheme.ink2, fontSize: 14),
                        ),
                      ),
                    ),
                  )
                : SliverList(
                    delegate: SliverChildBuilderDelegate(
                      (context, i) =>
                          _ObservationCard(obs: data.observations[i]),
                      childCount: data.observations.length,
                    ),
                  ),
          ),
        ],
      ),
    );
  }
}

// ── Night summary strip ───────────────────────────────────────────────────────

class _NightsStrip extends StatelessWidget {
  const _NightsStrip({required this.nights});
  final List<NightSummary> nights;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Padding(
          padding: EdgeInsets.fromLTRB(16, 4, 16, 10),
          child: Text(
            'RECENT NIGHTS',
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 11,
              fontWeight: FontWeight.w600,
              letterSpacing: 1.0,
              color: BSTheme.ink3,
            ),
          ),
        ),
        SizedBox(
          height: 90,
          child: ListView.builder(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12),
            itemCount: nights.length,
            itemBuilder: (context, i) => _NightTile(night: nights[i]),
          ),
        ),
        const SizedBox(height: 8),
        const Divider(height: 1, color: BSTheme.glassBorder),
        const SizedBox(height: 4),
      ],
    );
  }
}

class _NightTile extends StatelessWidget {
  const _NightTile({required this.night});
  final NightSummary night;

  @override
  Widget build(BuildContext context) {
    final clear = night.wasClear;
    final color = clear ? BSTheme.accent : BSTheme.ink3;

    String label = night.night;
    try {
      label = DateFormat('MMM d').format(DateTime.parse(night.night));
    } catch (_) {}

    return Container(
      width: 72,
      margin: const EdgeInsets.symmetric(horizontal: 4),
      padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 6),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(10),
        color: color.withValues(alpha: clear ? 0.10 : 0.04),
        border: Border.all(
            color: color.withValues(alpha: clear ? 0.28 : 0.12)),
      ),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(
            clear ? Icons.nights_stay_rounded : Icons.cloud_outlined,
            size: 18,
            color: color,
          ),
          const SizedBox(height: 5),
          Text(
            label,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 11,
              fontWeight: FontWeight.w600,
              color: color,
            ),
          ),
          const SizedBox(height: 3),
          Text(
            clear && night.receiptTitle.isNotEmpty
                ? night.receiptTitle
                : clear
                    ? '${night.nObservations} obs'
                    : 'clouded',
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            textAlign: TextAlign.center,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 9,
              color: BSTheme.ink3,
            ),
          ),
        ],
      ),
    );
  }
}

// ── Observation card ──────────────────────────────────────────────────────────

class _ObservationCard extends StatelessWidget {
  const _ObservationCard({required this.obs});
  final Observation obs;

  Color _magColor(double mag) {
    if (mag < 8) return BSTheme.warm;
    if (mag < 11) return BSTheme.accent;
    return BSTheme.ink2;
  }

  @override
  Widget build(BuildContext context) {
    final submitted = obs.aavsoSubmitted;
    final target = obs.targetName.isEmpty ? 'Unknown target' : obs.targetName;
    final magColor = _magColor(obs.magnitude);

    return GestureDetector(
      onTap: obs.targetName.isEmpty
          ? null
          : () => Navigator.push(
                context,
                MaterialPageRoute<void>(
                  builder: (_) =>
                      TargetDetailScreen(targetName: obs.targetName),
                ),
              ),
      child: Container(
        margin: const EdgeInsets.only(bottom: 10),
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(12),
          color: BSTheme.surface.withValues(alpha: 0.88),
          border: Border.all(color: magColor.withValues(alpha: 0.22)),
          boxShadow: [
            BoxShadow(
              color: Colors.black.withValues(alpha: 0.34),
              blurRadius: 18,
              offset: const Offset(0, 10),
            ),
          ],
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 46,
              height: 46,
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(10),
                color: magColor.withValues(alpha: 0.10),
                border: Border.all(color: magColor.withValues(alpha: 0.28)),
              ),
              child: Icon(Icons.star_rounded, size: 22, color: magColor),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    target,
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 15,
                      fontWeight: FontWeight.w800,
                      letterSpacing: 0,
                      color: BSTheme.ink,
                    ),
                  ),
                  const SizedBox(height: 5),
                  Row(
                    children: [
                      Text(
                        obs.magnitude.toStringAsFixed(3),
                        style: TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 22,
                          fontWeight: FontWeight.w800,
                          letterSpacing: 0,
                          color: magColor,
                          height: 1.0,
                        ),
                      ),
                      const SizedBox(width: 4),
                      const Text(
                        'mag',
                        style: TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 11,
                          color: BSTheme.ink3,
                          letterSpacing: 0,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Container(
                    padding: const EdgeInsets.all(9),
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(9),
                      color: BSTheme.ink.withValues(alpha: 0.035),
                      border: Border.all(color: BSTheme.glassBorder),
                    ),
                    child: Row(
                      children: [
                        Expanded(
                          child: _EvidenceDatum(
                            label: 'uncertainty',
                            value: '± ${obs.uncertainty.toStringAsFixed(3)}',
                          ),
                        ),
                        Expanded(
                          child: _EvidenceDatum(
                            label: 'node',
                            value: obs.nodeId.isEmpty ? 'unknown' : obs.nodeId,
                          ),
                        ),
                        Expanded(
                          child: _EvidenceDatum(
                            label: 'quality',
                            value: obs.qualityFlag.isEmpty
                                ? 'unmarked'
                                : obs.qualityFlag,
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 6,
                    runSpacing: 4,
                    children: [
                      if (obs.filter.isNotEmpty)
                        _Badge(
                          label: obs.filter.toUpperCase(),
                          color: BSTheme.accent,
                        ),
                      Semantics(
                        label: submitted
                            ? 'Submitted to AAVSO'
                            : 'Pending submission',
                        child: _Badge(
                          label: submitted ? 'AAVSO ✓' : 'PENDING',
                          color:
                              submitted ? BSTheme.success : BSTheme.warning,
                          icon: submitted ? null : Icons.schedule,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
            if (obs.targetName.isNotEmpty)
              const Padding(
                padding: EdgeInsets.only(top: 2),
                child: Icon(Icons.chevron_right, color: BSTheme.ink3, size: 18),
              ),
          ],
        ),
      ),
    );
  }
}

class _Badge extends StatelessWidget {
  const _Badge({required this.label, required this.color, this.icon});
  final String label;
  final Color color;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(6),
        color: color.withValues(alpha: 0.12),
        border: Border.all(color: color.withValues(alpha: 0.3)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 10, color: color),
            const SizedBox(width: 3),
          ],
          Text(
            label,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 10,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.8,
              color: color,
            ),
          ),
        ],
      ),
    );
  }
}

class _EvidenceDatum extends StatelessWidget {
  const _EvidenceDatum({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(
            fontFamily: 'Geist',
            fontSize: 9,
            color: BSTheme.ink3,
          ),
        ),
        const SizedBox(height: 2),
        Text(
          value,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(
            fontFamily: 'Geist',
            fontSize: 11,
            fontWeight: FontWeight.w800,
            color: BSTheme.ink2,
          ),
        ),
      ],
    );
  }
}
