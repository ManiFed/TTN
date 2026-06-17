import 'dart:math' as math;

import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:provider/provider.dart';
import 'package:vibration/vibration.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';

enum _ViewMode { chart, table, audio }

/// Accessibility-first light curve screen for one photometric target.
///
/// Three view modes — chart, data table, and audio description — are always
/// reachable via the segmented control at the top. In chart and table mode a
/// persistent bottom bar exposes "Hear" and "Feel" so the full picture is
/// never more than one tap away for non-visual users.
class TargetDetailScreen extends StatefulWidget {
  const TargetDetailScreen({super.key, required this.targetName});

  final String targetName;

  @override
  State<TargetDetailScreen> createState() => _TargetDetailScreenState();
}

class _TargetDetailScreenState extends State<TargetDetailScreen> {
  late Future<List<LightCurvePoint>> _future;
  _ViewMode _mode = _ViewMode.chart;

  final FlutterTts _tts = FlutterTts();
  bool _speaking = false;
  bool _hapticPlaying = false;

  @override
  void initState() {
    super.initState();
    _future = _load();
    _tts
      ..setLanguage('en-US')
      ..setSpeechRate(0.45)
      ..setPitch(1.0);
    _tts.setCompletionHandler(() {
      if (mounted) setState(() => _speaking = false);
    });
    _tts.setCancelHandler(() {
      if (mounted) setState(() => _speaking = false);
    });
  }

  @override
  void dispose() {
    _tts.stop();
    Vibration.cancel();
    super.dispose();
  }

  Future<List<LightCurvePoint>> _load() {
    final state = context.read<AppState>();
    return state.api.lightCurve(widget.targetName).catchError((e) {
      state.handleAuthError(e);
      throw e;
    });
  }

  // ── Audio description ──────────────────────────────────────────────────────

  String _buildDescription(List<LightCurvePoint> points) {
    if (points.isEmpty) {
      return 'No observations recorded for ${widget.targetName} yet.';
    }

    final sorted = [...points]..sort((a, b) => a.bjd.compareTo(b.bjd));
    final n = sorted.length;
    final latest = sorted.last;
    final daySpan = (latest.bjd - sorted.first.bjd).round();
    final mags = sorted.map((p) => p.magnitude).toList();
    final minMag = mags.reduce(math.min);
    final maxMag = mags.reduce(math.max);
    final range = (maxMag - minMag).toStringAsFixed(2);
    final brightestIdx = mags.indexOf(minMag);
    final daysAgo = (latest.bjd - sorted[brightestIdx].bjd).round();
    final submitted = sorted.where((p) => p.aavsoSubmitted).length;
    final good = sorted.where((p) => p.qualityFlag == 'good').length;

    String trend = '';
    if (sorted.length >= 4) {
      final firstAvg = sorted.take(2).map((p) => p.magnitude).reduce((a, b) => a + b) / 2;
      final lastAvg = sorted.reversed.take(2).map((p) => p.magnitude).reduce((a, b) => a + b) / 2;
      if (lastAvg < firstAvg - 0.1) {
        trend = 'Overall the star has been getting brighter. ';
      } else if (lastAvg > firstAvg + 0.1) {
        trend = 'Overall the star has been getting fainter. ';
      } else {
        trend = 'The brightness has been roughly steady. ';
      }
    }

    return '${widget.targetName}. '
        '$n observation${n == 1 ? '' : 's'} over $daySpan days. '
        'Most recent magnitude: ${latest.magnitude.toStringAsFixed(3)}, '
        'uncertainty plus or minus ${latest.uncertainty.toStringAsFixed(3)}. '
        'Quality: ${latest.qualityFlag}. '
        '${latest.aavsoSubmitted ? 'Submitted to AAVSO. ' : 'Pending AAVSO submission. '}'
        'Peak brightness was magnitude ${minMag.toStringAsFixed(2)}, '
        '${daysAgo == 0 ? 'reached tonight' : '$daysAgo day${daysAgo == 1 ? '' : 's'} ago'}. '
        'The star varied by $range magnitudes in total. '
        '$trend'
        '$submitted of $n measurements have been accepted by AAVSO. '
        '$good passed quality checks as good.';
  }

  Future<void> _toggleSpeech(List<LightCurvePoint> points) async {
    if (_speaking) {
      await _tts.stop();
      return; // completion handler clears _speaking
    }
    setState(() => _speaking = true);
    await _tts.speak(_buildDescription(points));
  }

  // ── Haptic light curve ─────────────────────────────────────────────────────

  Future<void> _playHaptic(List<LightCurvePoint> points) async {
    if (_hapticPlaying) {
      await Vibration.cancel();
      if (mounted) setState(() => _hapticPlaying = false);
      return;
    }

    final bool? canVibrate = await Vibration.hasVibrator();
    if (canVibrate != true) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
              content: Text('Haptic feedback is not available on this device.')),
        );
      }
      return;
    }

    final sorted = [...points]..sort((a, b) => a.bjd.compareTo(b.bjd));
    if (sorted.isEmpty) return;

    final mags = sorted.map((p) => p.magnitude).toList();
    final minMag = mags.reduce(math.min);
    final maxMag = mags.reduce(math.max);
    final magRange = maxMag - minMag;

    // Build pattern: alternating gap (no vibration) and pulse.
    // Brightness (inverted magnitude) drives pulse intensity.
    final List<int> pattern = [];
    final List<int> intensities = [];
    for (final pt in sorted) {
      final brightness =
          magRange > 0.01 ? (maxMag - pt.magnitude) / magRange : 0.5;
      final intensity = (60 + (brightness * 195).round()).clamp(60, 255);
      pattern.addAll([100, 220]); // gap ms, pulse ms
      intensities.addAll([0, intensity]);
    }

    setState(() => _hapticPlaying = true);
    final bool? hasAmplitude = await Vibration.hasAmplitudeControl();
    if (hasAmplitude == true) {
      await Vibration.vibrate(pattern: pattern, intensities: intensities);
    } else {
      // Fallback: encode brightness as pulse duration (longer = brighter).
      final fallback = <int>[];
      for (int i = 0; i < sorted.length; i++) {
        final brightness =
            magRange > 0.01 ? (maxMag - sorted[i].magnitude) / magRange : 0.5;
        fallback.addAll([100, 60 + (brightness * 340).round()]);
      }
      await Vibration.vibrate(pattern: fallback);
    }
    if (mounted) setState(() => _hapticPlaying = false);
  }

  // ── Chart ──────────────────────────────────────────────────────────────────

  Color _qualityColor(String q) => switch (q) {
        'good' => BSTheme.success,
        'acceptable' => BSTheme.warning,
        _ => BSTheme.danger,
      };

  Widget _buildChart(List<LightCurvePoint> points) {
    if (points.isEmpty) {
      return const Center(child: Text('No data yet.'));
    }

    final sorted = [...points]..sort((a, b) => a.bjd.compareTo(b.bjd));
    final origin = sorted.first.bjd;
    final spots =
        sorted.map((p) => FlSpot(p.bjd - origin, -p.magnitude)).toList();

    final mags = sorted.map((p) => p.magnitude).toList();
    final minMag = mags.reduce(math.min);
    final maxMag = mags.reduce(math.max);
    final pad = (maxMag - minMag) * 0.18 + 0.25;

    return Column(
      children: [
        Expanded(
          child: Semantics(
            label: 'Light curve chart for ${widget.targetName}. '
                '${sorted.length} data points. '
                'Magnitude ranges from ${minMag.toStringAsFixed(2)} '
                'to ${maxMag.toStringAsFixed(2)}. '
                'Tap the Hear button below for a spoken description, '
                'or switch to Table mode for row-by-row data.',
            child: Padding(
              padding: const EdgeInsets.fromLTRB(4, 16, 24, 8),
              child: LineChart(
                LineChartData(
                  minY: -maxMag - pad,
                  maxY: -minMag + pad,
                  gridData: FlGridData(
                    show: true,
                    horizontalInterval: 0.5,
                    getDrawingHorizontalLine: (_) =>
                        const FlLine(color: Colors.white10, strokeWidth: 0.8),
                    getDrawingVerticalLine: (_) =>
                        const FlLine(color: Colors.white10, strokeWidth: 0.8),
                  ),
                  borderData: FlBorderData(show: false),
                  titlesData: FlTitlesData(
                    rightTitles: const AxisTitles(
                        sideTitles: SideTitles(showTitles: false)),
                    topTitles: const AxisTitles(
                        sideTitles: SideTitles(showTitles: false)),
                    bottomTitles: AxisTitles(
                      axisNameWidget: const Padding(
                        padding: EdgeInsets.only(top: 6),
                        child: Text('Days',
                            style: TextStyle(
                                fontSize: 11, color: Colors.white54)),
                      ),
                      sideTitles: SideTitles(
                        showTitles: true,
                        reservedSize: 28,
                        getTitlesWidget: (v, _) => Text(
                          v.toInt().toString(),
                          style: const TextStyle(
                              fontSize: 11, color: Colors.white54),
                        ),
                      ),
                    ),
                    leftTitles: AxisTitles(
                      axisNameWidget: const Padding(
                        padding: EdgeInsets.only(bottom: 8),
                        child: RotatedBox(
                          quarterTurns: 3,
                          child: Text('Magnitude',
                              style: TextStyle(
                                  fontSize: 11, color: Colors.white54)),
                        ),
                      ),
                      sideTitles: SideTitles(
                        showTitles: true,
                        reservedSize: 46,
                        getTitlesWidget: (v, _) => Padding(
                          padding: const EdgeInsets.only(right: 4),
                          child: Text(
                            (-v).toStringAsFixed(1),
                            style: const TextStyle(
                                fontSize: 11, color: Colors.white54),
                          ),
                        ),
                      ),
                    ),
                  ),
                  lineBarsData: [
                    LineChartBarData(
                      spots: spots,
                      isCurved: false,
                      color: const Color(0xFF7DA9FF).withValues(alpha: 0.55),
                      barWidth: 1.2,
                      dotData: FlDotData(
                        show: true,
                        getDotPainter: (spot, pct, bar, idx) =>
                            FlDotCirclePainter(
                          radius: 4.5,
                          color: _qualityColor(sorted[idx].qualityFlag),
                          strokeColor: Colors.transparent,
                          strokeWidth: 0,
                        ),
                      ),
                    ),
                  ],
                  lineTouchData: LineTouchData(
                    touchTooltipData: LineTouchTooltipData(
                      getTooltipColor: (_) => const Color(0xFF1E2850),
                      getTooltipItems: (spots) => spots.map((s) {
                        final pt = sorted[s.spotIndex];
                        return LineTooltipItem(
                          'mag ${pt.magnitude.toStringAsFixed(3)}\n'
                          '±${pt.uncertainty.toStringAsFixed(3)}\n'
                          '${pt.aavsoSubmitted ? "AAVSO ✓" : "pending"}',
                          const TextStyle(
                              fontSize: 12,
                              color: Colors.white,
                              height: 1.5),
                        );
                      }).toList(),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
        // Legend
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              _LegendDot(color: BSTheme.success, label: 'Good'),
              const SizedBox(width: 16),
              _LegendDot(color: BSTheme.warning, label: 'Acceptable'),
              const SizedBox(width: 16),
              _LegendDot(color: BSTheme.danger, label: 'Poor'),
            ],
          ),
        ),
      ],
    );
  }

  // ── Table ──────────────────────────────────────────────────────────────────

  Widget _buildTable(List<LightCurvePoint> points) {
    if (points.isEmpty) {
      return const Center(child: Text('No data yet.'));
    }

    final sorted = [...points]..sort((a, b) => b.bjd.compareTo(a.bjd));

    String bjdToDate(double bjd) {
      // JD 2440587.5 = 1970-01-01 00:00:00 UTC
      final dt = DateTime.fromMillisecondsSinceEpoch(
        ((bjd - 2440587.5) * 86400000).round(),
        isUtc: true,
      );
      return '${dt.year}-'
          '${dt.month.toString().padLeft(2, '0')}-'
          '${dt.day.toString().padLeft(2, '0')}';
    }

    return Scrollbar(
      child: SingleChildScrollView(
        child: SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.all(16),
          child: DataTable(
            columnSpacing: 20,
            dataRowMinHeight: 48,
            dataRowMaxHeight: 56,
            headingTextStyle: const TextStyle(
              fontSize: 13,
              fontWeight: FontWeight.w700,
              color: Colors.white70,
            ),
            dataTextStyle: const TextStyle(fontSize: 13),
            columns: const [
              DataColumn(label: Text('Date')),
              DataColumn(label: Text('Magnitude'), numeric: true),
              DataColumn(label: Text('±Error'), numeric: true),
              DataColumn(label: Text('Filter')),
              DataColumn(label: Text('Quality')),
              DataColumn(label: Text('AAVSO')),
            ],
            rows: sorted.map((pt) {
              return DataRow(
                cells: [
                  DataCell(Text(bjdToDate(pt.bjd))),
                  DataCell(Text(pt.magnitude.toStringAsFixed(3))),
                  DataCell(Text(pt.uncertainty.toStringAsFixed(3))),
                  DataCell(
                      Text(pt.filter.isEmpty ? '—' : pt.filter.toUpperCase())),
                  DataCell(
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 8, vertical: 3),
                      decoration: BoxDecoration(
                        color: _qualityColor(pt.qualityFlag)
                            .withValues(alpha: 0.18),
                        borderRadius: BorderRadius.circular(6),
                      ),
                      child: Text(
                        pt.qualityFlag,
                        style: TextStyle(
                          color: _qualityColor(pt.qualityFlag),
                          fontWeight: FontWeight.w600,
                          fontSize: 12,
                        ),
                      ),
                    ),
                  ),
                  DataCell(
                    Semantics(
                      label: pt.aavsoSubmitted
                          ? 'Submitted to AAVSO'
                          : 'Not yet submitted',
                      child: Icon(
                        pt.aavsoSubmitted ? Icons.verified : Icons.schedule,
                        color: pt.aavsoSubmitted
                            ? BSTheme.success
                            : BSTheme.warning,
                        size: 22,
                      ),
                    ),
                  ),
                ],
              );
            }).toList(),
          ),
        ),
      ),
    );
  }

  // ── Audio panel ────────────────────────────────────────────────────────────

  Widget _buildAudioPanel(List<LightCurvePoint> points) {
    final description = _buildDescription(points);
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          Semantics(
            button: true,
            label: _speaking
                ? 'Stop audio description'
                : 'Play audio description for ${widget.targetName}',
            child: GestureDetector(
              onTap: () => _toggleSpeech(points),
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 200),
                width: 96,
                height: 96,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: _speaking
                      ? BSTheme.warning
                      : Theme.of(context).colorScheme.primary,
                ),
                child: Center(
                  child: Icon(
                    _speaking ? Icons.stop_rounded : Icons.volume_up_rounded,
                    size: 46,
                    color: _speaking
                        ? Colors.black87
                        : Theme.of(context).colorScheme.onPrimary,
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(height: 16),
          Text(
            _speaking ? 'Playing — tap to stop' : 'Tap to hear description',
            style: Theme.of(context).textTheme.bodyLarge,
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 28),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(20),
            decoration: BoxDecoration(
              color: const Color(0xFF161C3A),
              borderRadius: BorderRadius.circular(16),
            ),
            child: SelectableText(
              description,
              style: Theme.of(context)
                  .textTheme
                  .bodyLarge
                  ?.copyWith(height: 1.75),
            ),
          ),
        ],
      ),
    );
  }

  // ── Root build ─────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(widget.targetName)),
      body: AsyncView<List<LightCurvePoint>>(
        future: _future,
        onRefresh: () async => setState(() => _future = _load()),
        builder: (context, points) => Column(
          children: [
            // ── Mode selector ───────────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
              child: SegmentedButton<_ViewMode>(
                segments: const [
                  ButtonSegment(
                    value: _ViewMode.chart,
                    label: Text('Chart'),
                    icon: Icon(Icons.show_chart),
                  ),
                  ButtonSegment(
                    value: _ViewMode.table,
                    label: Text('Table'),
                    icon: Icon(Icons.table_rows_outlined),
                  ),
                  ButtonSegment(
                    value: _ViewMode.audio,
                    label: Text('Audio'),
                    icon: Icon(Icons.record_voice_over_outlined),
                  ),
                ],
                selected: {_mode},
                onSelectionChanged: (s) {
                  if (_speaking) {
                    _tts.stop();
                  }
                  setState(() => _mode = s.first);
                },
                style: const ButtonStyle(
                    visualDensity: VisualDensity.comfortable),
              ),
            ),

            // ── Summary chips ───────────────────────────────────────────────
            if (points.isNotEmpty) _SummaryBar(points: points),

            // ── Content ─────────────────────────────────────────────────────
            Expanded(
              child: switch (_mode) {
                _ViewMode.chart => _buildChart(points),
                _ViewMode.table => _buildTable(points),
                _ViewMode.audio => SingleChildScrollView(
                    padding: const EdgeInsets.only(bottom: 24),
                    child: _buildAudioPanel(points),
                  ),
              },
            ),

            // ── Persistent accessibility toolbar ────────────────────────────
            if (_mode != _ViewMode.audio)
              _AccessibilityBar(
                speaking: _speaking,
                hapticPlaying: _hapticPlaying,
                onHear: () => _toggleSpeech(points),
                onHaptic: () => _playHaptic(points),
              ),
          ],
        ),
      ),
    );
  }
}

// ── Supporting widgets ─────────────────────────────────────────────────────────

class _SummaryBar extends StatelessWidget {
  const _SummaryBar({required this.points});
  final List<LightCurvePoint> points;

  @override
  Widget build(BuildContext context) {
    final submitted = points.where((p) => p.aavsoSubmitted).length;
    final mags = points.map((p) => p.magnitude).toList();
    final minMag = mags.reduce(math.min);
    final maxMag = mags.reduce(math.max);

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      child: Wrap(
        spacing: 8,
        runSpacing: 6,
        children: [
          _SummaryChip(
            label: '${points.length} obs',
            icon: Icons.camera_alt_outlined,
            color: Colors.white70,
          ),
          _SummaryChip(
            label: '${minMag.toStringAsFixed(2)}–${maxMag.toStringAsFixed(2)} mag',
            icon: Icons.brightness_4_outlined,
            color: BSTheme.warning,
          ),
          _SummaryChip(
            label: '$submitted to AAVSO',
            icon: Icons.verified_outlined,
            color: BSTheme.success,
          ),
        ],
      ),
    );
  }
}

class _SummaryChip extends StatelessWidget {
  const _SummaryChip(
      {required this.label, required this.icon, required this.color});

  final String label;
  final IconData icon;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Chip(
      avatar: Icon(icon, size: 16, color: color),
      label: Text(label, style: TextStyle(color: color, fontSize: 13)),
      backgroundColor: color.withValues(alpha: 0.12),
      side: BorderSide(color: color.withValues(alpha: 0.28)),
      visualDensity: VisualDensity.compact,
      labelPadding: const EdgeInsets.symmetric(horizontal: 4),
    );
  }
}

class _LegendDot extends StatelessWidget {
  const _LegendDot({required this.color, required this.label});
  final Color color;
  final String label;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: '$label quality',
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 10,
            height: 10,
            decoration: BoxDecoration(shape: BoxShape.circle, color: color),
          ),
          const SizedBox(width: 5),
          Text(label,
              style:
                  const TextStyle(fontSize: 12, color: Colors.white54)),
        ],
      ),
    );
  }
}

/// Always-visible Hear / Feel buttons below the chart and table.
class _AccessibilityBar extends StatelessWidget {
  const _AccessibilityBar({
    required this.speaking,
    required this.hapticPlaying,
    required this.onHear,
    required this.onHaptic,
  });

  final bool speaking;
  final bool hapticPlaying;
  final VoidCallback onHear;
  final VoidCallback onHaptic;

  @override
  Widget build(BuildContext context) {
    final bottom = MediaQuery.of(context).padding.bottom;
    return Container(
      color: const Color(0xFF161C3A),
      padding: EdgeInsets.fromLTRB(16, 12, 16, 12 + bottom),
      child: Row(
        children: [
          Expanded(
            child: Semantics(
              label: speaking
                  ? 'Stop audio description'
                  : 'Hear audio description',
              button: true,
              child: OutlinedButton.icon(
                onPressed: onHear,
                icon: Icon(speaking ? Icons.stop : Icons.volume_up_outlined),
                label: Text(speaking ? 'Stop' : 'Hear'),
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(0, 52),
                  foregroundColor: speaking ? BSTheme.warning : Colors.white,
                  side: BorderSide(
                      color: speaking ? BSTheme.warning : Colors.white38),
                ),
              ),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Semantics(
              label: hapticPlaying
                  ? 'Stop haptic feedback'
                  : 'Feel the light curve',
              button: true,
              child: OutlinedButton.icon(
                onPressed: onHaptic,
                icon: Icon(hapticPlaying
                    ? Icons.stop
                    : Icons.vibration_outlined),
                label: Text(hapticPlaying ? 'Stop' : 'Feel'),
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(0, 52),
                  foregroundColor:
                      hapticPlaying ? BSTheme.warning : Colors.white,
                  side: BorderSide(
                      color: hapticPlaying
                          ? BSTheme.warning
                          : Colors.white38),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
