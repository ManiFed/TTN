import 'dart:ui';
import 'package:flutter/material.dart';

import '../theme.dart';

/// Shared "mission-control" glass language: frosted panels, glowing stat orbs,
/// animated count-ups and sparklines that float over the Aladin sky.

// ── Frosted glass panel ───────────────────────────────────────────────────────

/// A frosted, gradient-sheened card with a faint colored glow. The signature
/// surface of the redesigned app — used for every floating section.
class GlassPanel extends StatelessWidget {
  const GlassPanel({
    super.key,
    required this.child,
    this.glow = BSTheme.accent,
    this.padding = const EdgeInsets.all(16),
    this.radius = 24,
    this.blur = 18,
    this.onTap,
  });

  final Widget child;
  final Color glow;
  final EdgeInsets padding;
  final double radius;
  final double blur;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final panel = ClipRRect(
      borderRadius: BorderRadius.circular(radius),
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: blur, sigmaY: blur),
        child: Container(
          padding: padding,
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(radius),
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [
                glow.withValues(alpha: 0.10),
                const Color(0x14A0B9FF),
                const Color(0x08060E1E),
              ],
              stops: const [0.0, 0.4, 1.0],
            ),
            border: Border.all(color: BSTheme.glassBorder, width: 1),
            boxShadow: [
              BoxShadow(
                color: glow.withValues(alpha: 0.10),
                blurRadius: 28,
                spreadRadius: -8,
                offset: const Offset(0, 10),
              ),
            ],
          ),
          child: child,
        ),
      ),
    );

    if (onTap == null) return panel;
    return GestureDetector(onTap: onTap, behavior: HitTestBehavior.opaque, child: panel);
  }
}

// ── Section header with icon + glowing accent ─────────────────────────────────

class GlassSectionHeader extends StatelessWidget {
  const GlassSectionHeader({
    super.key,
    required this.icon,
    required this.label,
    this.detail,
    this.color = BSTheme.accent,
  });

  final IconData icon;
  final String label;
  final String? detail;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          width: 26,
          height: 26,
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(8),
            color: color.withValues(alpha: 0.14),
            border: Border.all(color: color.withValues(alpha: 0.32)),
          ),
          child: Icon(icon, size: 14, color: color),
        ),
        const SizedBox(width: 10),
        Text(
          label,
          style: const TextStyle(
            fontFamily: 'Geist',
            fontSize: 10,
            fontWeight: FontWeight.w700,
            letterSpacing: 2.4,
            color: BSTheme.ink2,
          ),
        ),
        const Spacer(),
        if (detail != null)
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(100),
              color: color.withValues(alpha: 0.10),
              border: Border.all(color: color.withValues(alpha: 0.24)),
            ),
            child: Text(
              detail!,
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 10,
                fontWeight: FontWeight.w600,
                letterSpacing: 0.2,
                color: color.withValues(alpha: 0.92),
              ),
            ),
          ),
      ],
    );
  }
}

// ── Animated count-up number ──────────────────────────────────────────────────

/// Counts from 0 → [value] once on build. Gives stat readouts a live,
/// "powering on" feel.
class CountUp extends StatelessWidget {
  const CountUp({
    super.key,
    required this.value,
    required this.style,
    this.duration = const Duration(milliseconds: 900),
  });

  final int value;
  final TextStyle style;
  final Duration duration;

  @override
  Widget build(BuildContext context) {
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0, end: value.toDouble()),
      duration: duration,
      curve: Curves.easeOutCubic,
      builder: (_, v, __) => Text(v.round().toString(), style: style),
    );
  }
}

// ── Stat orb — the hero metric tile ───────────────────────────────────────────

class StatOrb extends StatelessWidget {
  const StatOrb({
    super.key,
    required this.value,
    required this.label,
    required this.color,
    this.icon,
  });

  final int value;
  final String label;
  final Color color;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 12),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(18),
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [
            color.withValues(alpha: 0.16),
            color.withValues(alpha: 0.03),
          ],
        ),
        border: Border.all(color: color.withValues(alpha: 0.26)),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 15, color: color.withValues(alpha: 0.9)),
            const SizedBox(height: 6),
          ],
          CountUp(
            value: value,
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 24,
              fontWeight: FontWeight.w800,
              letterSpacing: -1.4,
              height: 1.0,
              color: BSTheme.ink,
              shadows: [
                Shadow(color: color.withValues(alpha: 0.65), blurRadius: 14),
              ],
            ),
          ),
          const SizedBox(height: 4),
          Text(
            label.toUpperCase(),
            textAlign: TextAlign.center,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 8.5,
              fontWeight: FontWeight.w600,
              letterSpacing: 1.2,
              color: BSTheme.ink3,
            ),
          ),
        ],
      ),
    );
  }
}

// ── Glowing sparkline ─────────────────────────────────────────────────────────

/// A small line chart with a glow + gradient fill, drawn from a list of values.
/// Used to visualise recent magnitudes as a live "signal" trace.
class Sparkline extends StatelessWidget {
  const Sparkline({
    super.key,
    required this.values,
    this.color = BSTheme.accent,
    this.height = 34,
  });

  final List<double> values;
  final Color color;
  final double height;

  @override
  Widget build(BuildContext context) {
    if (values.length < 2) return SizedBox(height: height);
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0, end: 1),
      duration: const Duration(milliseconds: 1100),
      curve: Curves.easeOutCubic,
      builder: (_, t, __) => SizedBox(
        height: height,
        width: double.infinity,
        child: CustomPaint(painter: _SparkPainter(values, color, t)),
      ),
    );
  }
}

class _SparkPainter extends CustomPainter {
  _SparkPainter(this.values, this.color, this.t);
  final List<double> values;
  final Color color;
  final double t;

  @override
  void paint(Canvas canvas, Size size) {
    final lo = values.reduce((a, b) => a < b ? a : b);
    final hi = values.reduce((a, b) => a > b ? a : b);
    final span = (hi - lo).abs() < 1e-9 ? 1.0 : (hi - lo);
    final dx = size.width / (values.length - 1);

    Offset pointAt(int i) {
      final x = dx * i;
      // Invert: brighter (lower magnitude) sits higher.
      final norm = (values[i] - lo) / span;
      final y = size.height * 0.15 + (size.height * 0.7) * norm;
      return Offset(x, y);
    }

    final reveal = (values.length - 1) * t;
    final path = Path();
    for (var i = 0; i <= reveal.floor() && i < values.length; i++) {
      final p = pointAt(i);
      i == 0 ? path.moveTo(p.dx, p.dy) : path.lineTo(p.dx, p.dy);
    }

    // Gradient fill under the curve.
    final fill = Path.from(path)
      ..lineTo(dx * reveal.floor(), size.height)
      ..lineTo(0, size.height)
      ..close();
    canvas.drawPath(
      fill,
      Paint()
        ..shader = LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [color.withValues(alpha: 0.28), color.withValues(alpha: 0.0)],
        ).createShader(Offset.zero & size),
    );

    // Glow pass + crisp line.
    canvas.drawPath(
      path,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 4
        ..strokeCap = StrokeCap.round
        ..color = color.withValues(alpha: 0.35)
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 4),
    );
    canvas.drawPath(
      path,
      Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2
        ..strokeCap = StrokeCap.round
        ..strokeJoin = StrokeJoin.round
        ..color = color,
    );

    // Leading dot.
    if (reveal.floor() < values.length) {
      final head = pointAt(reveal.floor());
      canvas.drawCircle(head, 3.2, Paint()..color = color);
      canvas.drawCircle(
        head,
        6,
        Paint()
          ..color = color.withValues(alpha: 0.4)
          ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 4),
      );
    }
  }

  @override
  bool shouldRepaint(_SparkPainter old) => old.t != t || old.values != values;
}

// ── Tiny chip ─────────────────────────────────────────────────────────────────

class GlowChip extends StatelessWidget {
  const GlowChip(this.label, {super.key, this.color = BSTheme.accent});
  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(5),
        color: color.withValues(alpha: 0.12),
        border: Border.all(color: color.withValues(alpha: 0.3)),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontFamily: 'Geist',
          fontSize: 9,
          fontWeight: FontWeight.w600,
          letterSpacing: 0.6,
          color: color,
        ),
      ),
    );
  }
}
