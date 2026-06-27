import 'dart:ui';
import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../config.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/aladin_sky.dart';
import '../widgets/glass.dart' show GrainOverlay, LiveDot;
import 'dashboard_tab.dart';
import 'me_screen.dart';
import 'nodes_tab.dart';
import 'notifications_tab.dart';
import 'observations_tab.dart';

/// The signed-in shell: Aladin sky behind every tab, frosted-glass chrome.
class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _index = 0;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final state = context.read<AppState>();
    if (state.pendingTab != null) {
      final tab = state.pendingTab!;
      state.pendingTab = null;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) setState(() => _index = tab);
      });
    }
  }

  static const _tabs = [
    (title: 'Tonight', icon: Icons.nightlight_outlined, sel: Icons.nightlight),
    (
      title: 'Telescopes',
      icon: Icons.satellite_alt_outlined,
      sel: Icons.satellite_alt
    ),
    (
      title: 'Observations',
      icon: Icons.show_chart_outlined,
      sel: Icons.show_chart
    ),
    (
      title: 'Alerts',
      icon: Icons.notifications_outlined,
      sel: Icons.notifications
    ),
  ];

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();

    // Show loading spinner while the node check is in-flight.
    if (!state.nodesLoaded) {
      return const Scaffold(
        backgroundColor: BSTheme.night,
        body: Center(child: CircularProgressIndicator()),
      );
    }

    // No linked node — show setup wall.
    if (!state.hasNode) {
      return const _SetupWall();
    }

    final name = state.member?.displayName ?? '';

    final pages = const [
      DashboardTab(),
      NodesTab(),
      ObservationsTab(),
      NotificationsTab(),
    ];

    return Stack(
      children: [
        // Live sky or painted glow background — shared by every tab.
        Positioned.fill(
          child: kIsWeb
              ? const AladinSky()
              : CustomPaint(painter: _NightGlowPainter()),
        ),
        // Dark veil — heavier than login so content stays readable.
        Positioned.fill(
          child: Container(color: const Color(0xBB000814)),
        ),
        // Film grain — organic texture over everything.
        const Positioned.fill(child: GrainOverlay()),
        Scaffold(
          backgroundColor: Colors.transparent,
          extendBodyBehindAppBar: true,
          extendBody: true,
          appBar: AppBar(
            backgroundColor: Colors.transparent,
            elevation: 0,
            flexibleSpace: ClipRect(
              child: BackdropFilter(
                filter: ImageFilter.blur(sigmaX: 24, sigmaY: 24),
                child: Container(
                  decoration: const BoxDecoration(
                    color: Color(0x1A060E1E),
                    border: Border(
                      bottom: BorderSide(color: BSTheme.glassBorder, width: 0.5),
                    ),
                  ),
                ),
              ),
            ),
            centerTitle: false,
            titleSpacing: 20,
            title: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const LiveDot(color: BSTheme.accent, size: 7),
                const SizedBox(width: 10),
                Text(
                  _tabs[_index].title,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                    letterSpacing: -0.5,
                    color: BSTheme.ink,
                  ),
                ),
              ],
            ),
            actions: [
              Padding(
                padding: const EdgeInsets.only(right: 16),
                child: PopupMenuButton<String>(
                  icon: Container(
                    width: 36,
                    height: 36,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      border: Border.all(color: BSTheme.glassBorder),
                      color: const Color(0x14A0B9FF),
                    ),
                    child: const Icon(
                      Icons.person_outline,
                      size: 17,
                      color: BSTheme.ink2,
                    ),
                  ),
                  tooltip: 'Account',
                  onSelected: (v) {
                    if (v == 'me') {
                      Navigator.of(context).push(
                        MaterialPageRoute<void>(
                          builder: (_) => const MeScreen(),
                        ),
                      );
                    } else if (v == 'signout') {
                      context.read<AppState>().signOut();
                    }
                  },
                  itemBuilder: (_) => [
                    PopupMenuItem<String>(
                      enabled: false,
                      child: Text(name.isEmpty ? 'Signed in' : name),
                    ),
                    const PopupMenuItem<String>(
                      value: 'me',
                      child: ListTile(
                        leading: Icon(Icons.person_outline),
                        title: Text('Me'),
                      ),
                    ),
                    const PopupMenuItem<String>(
                      value: 'signout',
                      child: ListTile(
                        leading: Icon(Icons.logout),
                        title: Text('Sign out'),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          body: IndexedStack(index: _index, children: pages),
          bottomNavigationBar: ClipRect(
            child: BackdropFilter(
              filter: ImageFilter.blur(sigmaX: 24, sigmaY: 24),
              child: Container(
                decoration: const BoxDecoration(
                  color: Color(0x1A060E1E),
                  border: Border(
                    top: BorderSide(color: BSTheme.glassBorder, width: 0.5),
                  ),
                ),
                child: SafeArea(
                  top: false,
                  child: SizedBox(
                    height: 64,
                    child: Row(
                      children: List.generate(_tabs.length, (i) {
                        final selected = _index == i;
                        final tab = _tabs[i];
                        return Expanded(
                          child: GestureDetector(
                            onTap: () => setState(() => _index = i),
                            behavior: HitTestBehavior.opaque,
                            child: AnimatedContainer(
                              duration: const Duration(milliseconds: 200),
                              curve: Curves.easeOut,
                              margin: const EdgeInsets.symmetric(
                                horizontal: 6,
                                vertical: 8,
                              ),
                              decoration: BoxDecoration(
                                borderRadius: BorderRadius.circular(12),
                                color: selected
                                    ? BSTheme.accent.withValues(alpha: 0.14)
                                    : Colors.transparent,
                              ),
                              child: Column(
                                mainAxisAlignment: MainAxisAlignment.center,
                                children: [
                                  Icon(
                                    selected ? tab.sel : tab.icon,
                                    color:
                                        selected ? BSTheme.accent : BSTheme.ink3,
                                    size: 20,
                                  ),
                                  const SizedBox(height: 3),
                                  Text(
                                    tab.title,
                                    style: TextStyle(
                                      fontFamily: 'Geist',
                                      fontSize: 10,
                                      fontWeight: FontWeight.w500,
                                      letterSpacing: 0.3,
                                      color: selected
                                          ? BSTheme.accent
                                          : BSTheme.ink3,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ),
                        );
                      }),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

// ── Setup wall ────────────────────────────────────────────────────────────────

class _SetupWall extends StatelessWidget {
  const _SetupWall();

  static String get _downloadUrl =>
      '${AppConfig.apiBase}/download/node-agent';

  static const _steps = [
    (
      icon: Icons.download_outlined,
      label: 'Download & install',
      detail: 'Run the installer on the Mac connected to your Seestar S50.',
    ),
    (
      icon: Icons.settings_outlined,
      label: 'Enter your activation code',
      detail:
          'Open the Node Agent dashboard, go to Settings → Cloud, and paste the code we emailed you.',
    ),
    (
      icon: Icons.nights_stay_outlined,
      label: 'Come back here',
      detail:
          'The software runs silently in the background. This app is where you see everything.',
    ),
  ];

  @override
  Widget build(BuildContext context) {
    return Stack(
      children: [
        Positioned.fill(
          child: kIsWeb
              ? const AladinSky()
              : CustomPaint(painter: _NightGlowPainter()),
        ),
        Positioned.fill(
          child: Container(color: const Color(0xBB000814)),
        ),
        const Positioned.fill(child: GrainOverlay()),
        Scaffold(
          backgroundColor: Colors.transparent,
          appBar: AppBar(
            backgroundColor: Colors.transparent,
            elevation: 0,
            actions: [
              TextButton(
                onPressed: () => context.read<AppState>().signOut(),
                child: const Text(
                  'Sign out',
                  style: TextStyle(color: BSTheme.ink3, fontSize: 13),
                ),
              ),
            ],
          ),
          body: SafeArea(
            child: Center(
              child: SingleChildScrollView(
                padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    // Icon
                    Container(
                      width: 72,
                      height: 72,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        border: Border.all(color: BSTheme.glassBorder),
                        color: const Color(0x14A0B9FF),
                      ),
                      child: const Icon(
                        Icons.satellite_alt_outlined,
                        color: BSTheme.accent,
                        size: 34,
                      ),
                    ),
                    const SizedBox(height: 24),
                    const Text(
                      'Connect your telescope',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 26,
                        fontWeight: FontWeight.w700,
                        letterSpacing: -0.5,
                        color: BSTheme.ink,
                      ),
                    ),
                    const SizedBox(height: 12),
                    const Text(
                      'Install the Node Software on your telescope\'s Mac.\nIt runs silently — this app is where you see everything.',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 15,
                        color: BSTheme.ink2,
                        height: 1.55,
                      ),
                    ),
                    const SizedBox(height: 36),

                    // Steps
                    ClipRRect(
                      borderRadius: BorderRadius.circular(16),
                      child: BackdropFilter(
                        filter: ImageFilter.blur(sigmaX: 20, sigmaY: 20),
                        child: Container(
                          decoration: BoxDecoration(
                            color: BSTheme.glassBg,
                            borderRadius: BorderRadius.circular(16),
                            border: Border.all(color: BSTheme.glassBorder),
                          ),
                          padding: const EdgeInsets.all(20),
                          child: Column(
                            children: List.generate(_steps.length, (i) {
                              final step = _steps[i];
                              return Padding(
                                padding: EdgeInsets.only(
                                    bottom: i < _steps.length - 1 ? 20 : 0),
                                child: Row(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Container(
                                      width: 36,
                                      height: 36,
                                      decoration: BoxDecoration(
                                        shape: BoxShape.circle,
                                        color: BSTheme.accent.withValues(alpha: 0.12),
                                      ),
                                      child: Center(
                                        child: Text(
                                          '${i + 1}',
                                          style: const TextStyle(
                                            fontFamily: 'Geist',
                                            fontSize: 14,
                                            fontWeight: FontWeight.w700,
                                            color: BSTheme.accent,
                                          ),
                                        ),
                                      ),
                                    ),
                                    const SizedBox(width: 14),
                                    Expanded(
                                      child: Column(
                                        crossAxisAlignment:
                                            CrossAxisAlignment.start,
                                        children: [
                                          Text(
                                            step.label,
                                            style: const TextStyle(
                                              fontFamily: 'Geist',
                                              fontSize: 14,
                                              fontWeight: FontWeight.w600,
                                              color: BSTheme.ink,
                                            ),
                                          ),
                                          const SizedBox(height: 3),
                                          Text(
                                            step.detail,
                                            style: const TextStyle(
                                              fontFamily: 'Geist',
                                              fontSize: 13,
                                              color: BSTheme.ink2,
                                              height: 1.5,
                                            ),
                                          ),
                                        ],
                                      ),
                                    ),
                                  ],
                                ),
                              );
                            }),
                          ),
                        ),
                      ),
                    ),

                    const SizedBox(height: 28),

                    // Download button
                    SizedBox(
                      width: double.infinity,
                      child: FilledButton.icon(
                        onPressed: () => launchUrl(
                          Uri.parse(_downloadUrl),
                          mode: LaunchMode.externalApplication,
                        ),
                        icon: const Icon(Icons.download_outlined, size: 18),
                        label: const Text('Download Node Software'),
                        style: FilledButton.styleFrom(
                          backgroundColor: BSTheme.btnPrimary,
                          foregroundColor: BSTheme.btnPrimaryFg,
                          padding: const EdgeInsets.symmetric(vertical: 15),
                          textStyle: const TextStyle(
                            fontFamily: 'Geist',
                            fontSize: 15,
                            fontWeight: FontWeight.w600,
                          ),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(12),
                          ),
                        ),
                      ),
                    ),

                    const SizedBox(height: 16),

                    // Already installed? Re-check.
                    TextButton(
                      onPressed: () => context.read<AppState>().refreshNodes(),
                      child: const Text(
                        'Already installed and registered? Tap to refresh',
                        style: TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 13,
                          color: BSTheme.ink3,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

// ── Night glow background ─────────────────────────────────────────────────────

class _NightGlowPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    canvas.drawRect(
      Offset.zero & size,
      Paint()
        ..shader = RadialGradient(
          center: Alignment.topCenter,
          radius: 0.9,
          colors: [
            const Color(0xFF8FD9FF).withValues(alpha: 0.08),
            Colors.transparent,
          ],
        ).createShader(Offset.zero & size),
    );
    canvas.drawRect(
      Offset.zero & size,
      Paint()
        ..shader = RadialGradient(
          center: const Alignment(-1.0, 1.2),
          radius: 0.8,
          colors: [
            const Color(0xFFFFC07A).withValues(alpha: 0.05),
            Colors.transparent,
          ],
        ).createShader(Offset.zero & size),
    );
  }

  @override
  bool shouldRepaint(_NightGlowPainter old) => false;
}
