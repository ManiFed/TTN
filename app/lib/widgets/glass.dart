import 'dart:math' as math;
import 'dart:ui';
import 'package:flutter/material.dart';

import '../theme.dart';

/// Shared "mission-control" glass language: frosted panels, glowing stat orbs,
/// animated count-ups and sparklines that float over the Aladin sky.

// ── Gradient transform for kicker-shine sweep ─────────────────────────────────

class _SweepTransform implements GradientTransform {
  const _SweepTransform(this.progress);
  final double progress; // 0→1

  @override
  Matrix4? transform(Rect bounds, {TextDirection? textDirection}) {
    // Shift the gradient from 2× width offset → -1× width, left to right.
    final shift = bounds.width * (2.0 - progress * 3.0);
    return Matrix4.translationValues(shift, 0, 0);
  }
}

// ── Kicker-shine animated label ───────────────────────────────────────────────

class _KickerLabel extends StatefulWidget {
  const _KickerLabel(this.label, {this.style});
  final String label;
  final TextStyle? style;

  @override
  State<_KickerLabel> createState() => _KickerLabelState();
}

class _KickerLabelState extends State<_KickerLabel>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 5000),
    )..repeat();
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final base = widget.style ??
        const TextStyle(
          fontFamily: 'Geist',
          fontSize: 10,
          fontWeight: FontWeight.w700,
          letterSpacing: 2.4,
          color: BSTheme.ink2,
        );

    return AnimatedBuilder(
      animation: _ctrl,
      builder: (_, __) => ShaderMask(
        blendMode: BlendMode.srcIn,
        shaderCallback: (bounds) => LinearGradient(
          colors: const [
            BSTheme.accent,
            Color(0xFFD4F0FF),
            BSTheme.accent,
          ],
          stops: const [0.0, 0.5, 1.0],
          tileMode: TileMode.clamp,
          transform: _SweepTransform(_ctrl.value),
        ).createShader(bounds),
        child: Text(widget.label, style: base.copyWith(color: Colors.white)),
      ),
    );
  }
}

// ── Frosted glass panel ───────────────────────────────────────────────────────

/// A frosted, gradient-sheened card with a faint colored glow. Breathes slowly,
/// with a top-edge gloss and specular highlight matching the tour.html aesthetic.
class GlassPanel extends StatefulWidget {
  const GlassPanel({
    super.key,
    required this.child,
    this.glow = BSTheme.accent,
    this.padding = const EdgeInsets.all(16),
    this.radius = 24,
    this.blur = 14,
    this.onTap,
  });

  final Widget child;
  final Color glow;
  final EdgeInsets padding;
  final double radius;
  final double blur;
  final VoidCallback? onTap;

  @override
  State<GlassPanel> createState() => _GlassPanelState();
}

class _GlassPanelState extends State<GlassPanel>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;
  late final Animation<double> _breathe;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 12000),
    )..repeat();
    // Sine-wave: oscillates 0.045 → 0.065
    _breathe = _ctrl.drive(
      Tween(begin: 0.0, end: 1.0).chain(CurveTween(curve: Curves.easeInOut)),
    );
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _breathe,
      builder: (_, child) {
        final alpha = 0.045 + (_breathe.value < 0.5
            ? _breathe.value * 2
            : (1.0 - _breathe.value) * 2) * 0.020;

        final panel = ClipRRect(
          borderRadius: BorderRadius.circular(widget.radius),
          child: BackdropFilter(
            filter: ImageFilter.compose(
              outer: ImageFilter.blur(sigmaX: widget.blur, sigmaY: widget.blur),
              inner: const ColorFilter.matrix(<double>[
                // Saturation ×1.9 + brightness ×1.08
                0.607, 0.513, 0.153, 0, 20,
                0.175, 1.220, 0.153, 0, 20,
                0.175, 0.513, 1.075, 0, 20,
                0,     0,     0,     1, 0,
              ]),
            ),
            child: Stack(
              children: [
                // Base container with breathing gradient
                Container(
                  padding: widget.padding,
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(widget.radius),
                    gradient: LinearGradient(
                      begin: Alignment.topLeft,
                      end: Alignment.bottomRight,
                      colors: [
                        widget.glow.withValues(alpha: 0.10),
                        Color.fromRGBO(160, 185, 255, alpha),
                        const Color(0x08060E1E),
                      ],
                      stops: const [0.0, 0.4, 1.0],
                    ),
                    border: Border.all(color: BSTheme.glassBorder, width: 1),
                    boxShadow: [
                      BoxShadow(
                        color: widget.glow.withValues(alpha: 0.10),
                        blurRadius: 28,
                        spreadRadius: -8,
                        offset: const Offset(0, 10),
                      ),
                    ],
                  ),
                  child: child,
                ),
                // Top-edge gloss (::before equivalent)
                Positioned.fill(
                  child: IgnorePointer(
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(widget.radius),
                      child: Align(
                        alignment: Alignment.topCenter,
                        child: FractionallySizedBox(
                          heightFactor: 0.45,
                          widthFactor: 1.0,
                          child: Container(
                            decoration: const BoxDecoration(
                              gradient: LinearGradient(
                                begin: Alignment.topCenter,
                                end: Alignment.bottomCenter,
                                colors: [
                                  Color(0x24FFFFFF),
                                  Color(0x00FFFFFF),
                                ],
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
                // Specular highlight top-right (::after equivalent)
                Positioned(
                  top: -20,
                  right: -16,
                  width: 120,
                  height: 100,
                  child: IgnorePointer(
                    child: Container(
                      decoration: const BoxDecoration(
                        gradient: RadialGradient(
                          colors: [
                            Color(0x1FFFFFFF),
                            Color(0x00FFFFFF),
                          ],
                          radius: 0.72,
                        ),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        );

        if (widget.onTap == null) return panel;
        return GestureDetector(
          onTap: widget.onTap,
          behavior: HitTestBehavior.opaque,
          child: panel,
        );
      },
      child: widget.child,
    );
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
        _KickerLabel(
          label,
          style: TextStyle(
            fontFamily: 'Geist',
            fontSize: 10,
            fontWeight: FontWeight.w700,
            letterSpacing: 2.4,
            color: color.withValues(alpha: 0.92),
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
    this.onTap,
  });

  final int value;
  final String label;
  final Color color;
  final IconData? icon;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final orb = Container(
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
              fontFeatures: const [FontFeature.tabularFigures()],
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
    if (onTap == null) return orb;
    return GestureDetector(onTap: onTap, behavior: HitTestBehavior.opaque, child: orb);
  }
}

// ── Glowing sparkline ─────────────────────────────────────────────────────────

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

// ── Scale-based live pulse dot ────────────────────────────────────────────────

/// Pulsing dot that breathes via *scale* (not opacity), matching tour.html's
/// `livepulse` keyframe: scale 1.0 → 0.78 → 1.0 on a 2.4s cycle.
class LiveDot extends StatefulWidget {
  const LiveDot({super.key, required this.color, this.size = 7});
  final Color color;
  final double size;

  @override
  State<LiveDot> createState() => _LiveDotState();
}

class _LiveDotState extends State<LiveDot> with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;
  late final Animation<double> _scale;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2400),
    )..repeat(reverse: true);
    _scale = Tween<double>(begin: 1.0, end: 0.78).animate(
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
    return AnimatedBuilder(
      animation: _scale,
      builder: (_, __) => Transform.scale(
        scale: _scale.value,
        child: Container(
          width: widget.size,
          height: widget.size,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: widget.color,
            boxShadow: [
              BoxShadow(
                color: widget.color.withValues(alpha: 0.9),
                blurRadius: 8,
                spreadRadius: 1.2,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// ── Film grain painter ────────────────────────────────────────────────────────

/// Draws ~1200 random 1×1 pixel dots at 2.2% opacity. Static — never repaints.
/// Used as a full-screen overlay to add the organic texture that separates
/// handcrafted UIs from flat, AI-generated ones.
class GrainOverlay extends StatelessWidget {
  const GrainOverlay({super.key});

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      child: CustomPaint(painter: _GrainPainter()),
    );
  }
}

class _GrainPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final rng = math.Random(42);
    final paint = Paint()..color = const Color(0x06F2F5FF); // ~2.2% opacity
    for (var i = 0; i < 1200; i++) {
      final x = rng.nextDouble() * size.width;
      final y = rng.nextDouble() * size.height;
      canvas.drawRect(Rect.fromLTWH(x, y, 1, 1), paint);
    }
  }

  @override
  bool shouldRepaint(_GrainPainter old) => false;
}
