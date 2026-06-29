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
import 'more_tab.dart';
import 'nodes_tab.dart';
import 'notifications_tab.dart';
import 'observations_tab.dart';

/// The signed-in shell: operational workspace with alerts in the top bar.
class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _index = 0;

  void _applyPendingTab(AppState state) {
    final tab = state.takePendingTab();
    if (tab == null) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      if (tab >= _tabs.length) {
        _showAlertsSheet();
        return;
      }
      if (tab != _index) setState(() => _index = tab);
    });
  }

  Future<void> _showAlertsSheet() async {
    context.read<AppState>().refreshUnreadNotifications();
    await showDialog<void>(
      context: context,
      builder: (context) {
        return Dialog(
          backgroundColor: BSTheme.surface,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(8),
            side: const BorderSide(color: BSTheme.glassBorder),
          ),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 520, maxHeight: 620),
            child: const NotificationsTab(),
          ),
        );
      },
    );
    if (mounted) context.read<AppState>().refreshUnreadNotifications();
  }

  static const _tabs = [
    (title: 'Tonight', icon: Icons.nightlight_outlined, sel: Icons.nightlight),
    (
      title: 'Telescopes',
      icon: Icons.satellite_alt_outlined,
      sel: Icons.satellite_alt
    ),
    (
      title: 'History',
      icon: Icons.history_outlined,
      sel: Icons.history
    ),
    (title: 'Me', icon: Icons.person_outline, sel: Icons.person),
    (
      title: 'More',
      icon: Icons.more_horiz,
      sel: Icons.more_horiz
    ),
  ];

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    _applyPendingTab(state);

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

    final pages = [
      DashboardTab(onNavigateToTab: (_) => _showAlertsSheet()),
      const NodesTab(),
      const ObservationsTab(),
      const MeScreen(showAppBar: false),
      const MoreTab(),
    ];

    return Stack(
      children: [
        Positioned.fill(
          child: Container(color: BSTheme.night),
        ),
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
                    color: Color(0xD9030404),
                    border: Border(
                      bottom: BorderSide(color: BSTheme.glassBorder, width: 1),
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
                  'TELESCOPE // ${_tabs[_index].title.toUpperCase()}',
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 14,
                    fontWeight: FontWeight.w900,
                    letterSpacing: 1.2,
                    color: BSTheme.ink,
                  ),
                ),
              ],
            ),
            actions: [
              Padding(
                padding: const EdgeInsets.only(right: 8),
                child: IconButton(
                  tooltip: 'Alerts',
                  onPressed: _showAlertsSheet,
                  icon: Stack(
                    clipBehavior: Clip.none,
                    children: [
                      const Icon(Icons.notifications_none, color: BSTheme.ink2),
                      if (state.unreadNotifications > 0)
                        Positioned(
                          right: -2,
                          top: -2,
                          child: _UnreadBadge(
                            text: state.unreadNotifications > 9
                                ? '9+'
                                : '${state.unreadNotifications}',
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              Padding(
                padding: const EdgeInsets.only(right: 16),
                child: PopupMenuButton<String>(
                  icon: Container(
                    width: 36,
                    height: 36,
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(4),
                      border: Border.all(color: BSTheme.glassBorder),
                      color: BSTheme.ink.withValues(alpha: 0.04),
                    ),
                    child: const Icon(
                      Icons.person_outline,
                      size: 17,
                      color: BSTheme.ink2,
                    ),
                  ),
                  tooltip: 'Account',
                  onSelected: (v) {
                    if (v == 'signout') {
                      context.read<AppState>().signOut();
                    }
                  },
                  itemBuilder: (_) => [
                    PopupMenuItem<String>(
                      enabled: false,
                      child: Text(name.isEmpty ? 'Signed in' : name),
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
          body: LayoutBuilder(
            builder: (context, constraints) {
              final wide = constraints.maxWidth >= 720;
              final content = IndexedStack(index: _index, children: pages);
              if (!wide) return content;
              return Row(
                children: [
                  _ReadinessRail(
                    index: _index,
                    tabs: _tabs,
                    nodesReady: state.hasNode,
                    onSelect: (i) {
                      setState(() => _index = i);
                    },
                  ),
                  Expanded(child: content),
                ],
              );
            },
          ),
          bottomNavigationBar: LayoutBuilder(
            builder: (context, constraints) {
              if (constraints.maxWidth >= 720) return const SizedBox.shrink();
              return ClipRect(
                child: BackdropFilter(
                  filter: ImageFilter.blur(sigmaX: 24, sigmaY: 24),
                  child: Container(
                    decoration: const BoxDecoration(
                      color: Color(0xF2030404),
                      border: Border(
                        top: BorderSide(
                          color: BSTheme.glassBorder,
                          width: 1,
                        ),
                      ),
                    ),
                    child: SafeArea(
                      top: false,
                      child: SizedBox(
                        height: 66,
                        child: Row(
                          children: List.generate(_tabs.length, (i) {
                            final selected = _index == i;
                            final tab = _tabs[i];
                            return Expanded(
                              child: _BottomNavItem(
                                selected: selected,
                                title: tab.title,
                                icon: selected ? tab.sel : tab.icon,
                                showBadge: false,
                                badgeText: state.unreadNotifications > 9
                                    ? '9+'
                                    : '${state.unreadNotifications}',
                                onTap: () {
                                  setState(() => _index = i);
                                },
                              ),
                            );
                          }),
                        ),
                      ),
                    ),
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

class _ReadinessRail extends StatelessWidget {
  const _ReadinessRail({
    required this.index,
    required this.tabs,
    required this.nodesReady,
    required this.onSelect,
  });

  final int index;
  final List<({IconData icon, IconData sel, String title})> tabs;
  final bool nodesReady;
  final ValueChanged<int> onSelect;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 104,
      margin: EdgeInsets.only(
        top: MediaQuery.of(context).padding.top + kToolbarHeight,
      ),
      decoration: const BoxDecoration(
        color: Color(0xF0030404),
        border: Border(
          right: BorderSide(color: BSTheme.glassBorder, width: 1),
        ),
      ),
      child: SafeArea(
        top: false,
        child: Column(
          children: [
            const SizedBox(height: 14),
            _RailStateBadge(
              label: nodesReady ? 'ONLINE' : 'SETUP',
              color: nodesReady ? BSTheme.success : BSTheme.warm,
            ),
            const SizedBox(height: 14),
            Expanded(
              child: ListView.builder(
                padding: const EdgeInsets.symmetric(horizontal: 10),
                itemCount: tabs.length,
                itemBuilder: (context, i) => _RailItem(
                  selected: index == i,
                  icon: index == i ? tabs[i].sel : tabs[i].icon,
                  label: tabs[i].title,
                  badge: null,
                  onTap: () => onSelect(i),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _RailStateBadge extends StatelessWidget {
  const _RailStateBadge({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 78,
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 7),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(3),
        color: color.withValues(alpha: 0.10),
        border: Border.all(color: color.withValues(alpha: 0.35)),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontFamily: 'Geist',
          fontSize: 10,
          fontWeight: FontWeight.w900,
          letterSpacing: 0.8,
          color: color,
        ),
      ),
    );
  }
}

class _RailItem extends StatelessWidget {
  const _RailItem({
    required this.selected,
    required this.icon,
    required this.label,
    required this.onTap,
    this.badge,
  });

  final bool selected;
  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final int? badge;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Material(
        color: selected ? BSTheme.ink.withValues(alpha: 0.07) : Colors.transparent,
        borderRadius: BorderRadius.circular(3),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(3),
          child: Container(
            height: 68,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(3),
              border: Border.all(
                color: selected
                    ? BSTheme.accent.withValues(alpha: 0.34)
                    : Colors.transparent,
              ),
            ),
            child: Stack(
              alignment: Alignment.center,
              children: [
                if (selected)
                  Positioned(
                    left: 0,
                    top: 10,
                    bottom: 10,
                    child: Container(width: 3, color: BSTheme.accent),
                  ),
                Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Icon(
                      icon,
                      color: selected ? BSTheme.accent : BSTheme.ink3,
                      size: 21,
                    ),
                    const SizedBox(height: 4),
                    Text(
                      label,
                      textAlign: TextAlign.center,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 9,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0,
                        color: selected ? BSTheme.ink : BSTheme.ink3,
                      ),
                    ),
                  ],
                ),
                if (badge != null)
                  Positioned(
                    right: 8,
                    top: 7,
                    child: _UnreadBadge(text: badge! > 9 ? '9+' : '$badge'),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _BottomNavItem extends StatelessWidget {
  const _BottomNavItem({
    required this.selected,
    required this.title,
    required this.icon,
    required this.showBadge,
    required this.badgeText,
    required this.onTap,
  });

  final bool selected;
  final String title;
  final IconData icon;
  final bool showBadge;
  final String badgeText;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      behavior: HitTestBehavior.opaque,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 180),
        curve: Curves.easeOut,
        margin: const EdgeInsets.symmetric(horizontal: 5, vertical: 7),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(3),
          color: selected ? BSTheme.ink.withValues(alpha: 0.07) : Colors.transparent,
          border: Border.all(
            color: selected
                ? BSTheme.accent.withValues(alpha: 0.34)
                : Colors.transparent,
          ),
        ),
        child: Stack(
          alignment: Alignment.center,
          children: [
            if (selected)
              Positioned(
                top: 0,
                left: 12,
                right: 12,
                child: Container(height: 3, color: BSTheme.accent),
              ),
            Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Stack(
                  clipBehavior: Clip.none,
                  children: [
                    Icon(
                      icon,
                      color: selected ? BSTheme.accent : BSTheme.ink3,
                      size: 20,
                    ),
                    if (showBadge)
                      Positioned(
                        right: -9,
                        top: -7,
                        child: _UnreadBadge(text: badgeText),
                      ),
                  ],
                ),
                const SizedBox(height: 4),
                Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 10,
                    fontWeight: FontWeight.w800,
                    letterSpacing: 0,
                    color: selected ? BSTheme.ink : BSTheme.ink3,
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _UnreadBadge extends StatelessWidget {
  const _UnreadBadge({required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
      constraints: const BoxConstraints(minWidth: 15, minHeight: 15),
      decoration: BoxDecoration(
        color: BSTheme.danger,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: BSTheme.surface, width: 1.5),
      ),
      alignment: Alignment.center,
      child: Text(
        text,
        style: const TextStyle(
          fontFamily: 'Geist',
          fontSize: 8,
          fontWeight: FontWeight.w800,
          color: Colors.white,
          height: 1,
        ),
      ),
    );
  }
}

// ── Setup wall ────────────────────────────────────────────────────────────────

class _SetupWall extends StatefulWidget {
  const _SetupWall();

  @override
  State<_SetupWall> createState() => _SetupWallState();
}

class _SetupWallState extends State<_SetupWall> {
  bool _downloaded = false;

  static String get _downloadUrl =>
      '${AppConfig.apiBase}/download/node-agent';

  static const _stepsBeforeDownload = [
    (
      icon: Icons.download_outlined,
      label: 'Download & install',
      detail: 'Run the installer on the Mac connected to your telescope.',
    ),
    (
      icon: Icons.sync_outlined,
      label: 'Node software starts',
      detail: 'The installer starts the node software automatically.',
    ),
    (
      icon: Icons.link_outlined,
      label: 'Connect your telescope',
      detail:
          'Come back here and tap "Connect telescope" to link your account.',
    ),
  ];

  static const _stepsAfterDownload = [
    (
      icon: Icons.download_done_outlined,
      label: 'Run the installer',
      detail: 'Open the downloaded .pkg and follow the steps.',
    ),
    (
      icon: Icons.sync_outlined,
      label: 'Node software starts',
      detail: 'The installer starts the node software automatically when done.',
    ),
    (
      icon: Icons.link_outlined,
      label: 'Come back here and tap Connect',
      detail:
          'Tap "Connect telescope" below, then paste the activation code into the Node Agent dashboard.',
    ),
  ];

  Future<void> _onDownload() async {
    await launchUrl(
      Uri.parse(_downloadUrl),
      mode: LaunchMode.externalApplication,
    );
    if (mounted) setState(() => _downloaded = true);
  }

  Future<void> _onConnect() async {
    final claimed = await showClaimSheet(context);
    if (claimed && mounted) {
      context.read<AppState>().refreshNodes();
    }
  }
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
                        letterSpacing: 0,
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
                            children: List.generate(3, (i) {
                              final steps = _downloaded
                                  ? _stepsAfterDownload
                                  : _stepsBeforeDownload;
                              final step = steps[i];
                              return Padding(
                                padding: EdgeInsets.only(
                                    bottom: i < 2 ? 20 : 0),
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

                    if (!_downloaded) ...[
                      // Before download: download is primary
                      SizedBox(
                        width: double.infinity,
                        child: FilledButton.icon(
                          onPressed: _onDownload,
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
                      const SizedBox(height: 12),
                      SizedBox(
                        width: double.infinity,
                        child: OutlinedButton.icon(
                          onPressed: _onConnect,
                          icon: const Icon(Icons.link_outlined, size: 18),
                          label: const Text('Already installed - connect telescope'),
                          style: OutlinedButton.styleFrom(
                            foregroundColor: BSTheme.accent,
                            side: BorderSide(color: BSTheme.accent.withValues(alpha: 0.4)),
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
                    ] else ...[
                      // After download: connect is primary
                      SizedBox(
                        width: double.infinity,
                        child: FilledButton.icon(
                          onPressed: _onConnect,
                          icon: const Icon(Icons.link_outlined, size: 18),
                          label: const Text('Connect telescope'),
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
                      const SizedBox(height: 8),
                      TextButton.icon(
                        onPressed: _onDownload,
                        icon: const Icon(Icons.download_outlined, size: 15),
                        label: const Text('Download again'),
                        style: TextButton.styleFrom(
                          foregroundColor: BSTheme.ink3,
                          textStyle: const TextStyle(
                            fontFamily: 'Geist',
                            fontSize: 13,
                          ),
                        ),
                      ),
                    ],

                    const SizedBox(height: 4),

                    // Already installed? Re-check.
                    TextButton(
                      onPressed: () => context.read<AppState>().refreshNodes(),
                      child: const Text(
                        'Already connected? Tap to refresh',
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
            BSTheme.accent.withValues(alpha: 0.035),
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
            BSTheme.surface2.withValues(alpha: 0.16),
            Colors.transparent,
          ],
        ).createShader(Offset.zero & size),
    );
  }

  @override
  bool shouldRepaint(_NightGlowPainter old) => false;
}
