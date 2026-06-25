import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';
import 'target_detail_screen.dart';

/// Recent photometric measurements across the member's telescopes.
class ObservationsTab extends StatefulWidget {
  const ObservationsTab({super.key});

  @override
  State<ObservationsTab> createState() => _ObservationsTabState();
}

class _ObservationsTabState extends State<ObservationsTab> {
  late Future<List<Observation>> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<List<Observation>> _load() {
    final state = context.read<AppState>();
    return state.api.observations().catchError((e) {
      state.handleAuthError(e);
      throw e;
    });
  }

  Future<void> _refresh() async => setState(() => _future = _load());

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.of(context).padding.top + kToolbarHeight;
    final bottom = MediaQuery.of(context).padding.bottom + 64;

    return AsyncView<List<Observation>>(
      future: _future,
      onRefresh: _refresh,
      isEmpty: (list) => list.isEmpty,
      emptyMessage: 'No observations yet.\nThey appear here after a clear night.',
      builder: (context, obs) => ListView.builder(
        padding: EdgeInsets.fromLTRB(16, top + 8, 16, bottom + 16),
        itemCount: obs.length,
        itemBuilder: (context, i) => _ObservationCard(obs: obs[i]),
      ),
    );
  }
}

// ── Observation card ──────────────────────────────────────────────────────────

class _ObservationCard extends StatelessWidget {
  const _ObservationCard({required this.obs});
  final Observation obs;

  // Color-code by brightness: lower magnitude = brighter.
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
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(20),
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [
              magColor.withValues(alpha: 0.10),
              const Color(0x12A0B9FF),
              const Color(0x08060E1E),
            ],
            stops: const [0.0, 0.45, 1.0],
          ),
          border: Border.all(color: BSTheme.glassBorder),
          boxShadow: [
            BoxShadow(
              color: magColor.withValues(alpha: 0.10),
              blurRadius: 22,
              spreadRadius: -8,
              offset: const Offset(0, 8),
            ),
          ],
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Left: star glyph with magnitude-tinted glow
            Container(
              width: 46,
              height: 46,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: magColor.withValues(alpha: 0.10),
                border: Border.all(color: magColor.withValues(alpha: 0.28)),
              ),
              child: Icon(Icons.star_rounded, size: 22, color: magColor),
            ),
            const SizedBox(width: 14),
            // Middle: target name + magnitude readout
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    target,
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 15,
                      fontWeight: FontWeight.w600,
                      letterSpacing: -0.3,
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
                          letterSpacing: -1.0,
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
                          letterSpacing: 0.5,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    '± ${obs.uncertainty.toStringAsFixed(3)}',
                    style: const TextStyle(
                      fontFamily: 'Geist',
                      fontSize: 12,
                      color: BSTheme.ink3,
                    ),
                  ),
                  const SizedBox(height: 8),
                  // Badges row
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
