import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';

/// Member alerts (night summaries, AAVSO acceptances, system messages).
class NotificationsTab extends StatefulWidget {
  const NotificationsTab({super.key});

  @override
  State<NotificationsTab> createState() => _NotificationsTabState();
}

class _NotificationsTabState extends State<NotificationsTab> {
  late Future<(List<AppNotification>, int)> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<(List<AppNotification>, int)> _load() {
    final state = context.read<AppState>();
    return state.api.notifications().catchError((e) {
      state.handleAuthError(e);
      throw e;
    });
  }

  Future<void> _refresh() async => setState(() => _future = _load());

  Future<void> _markRead(AppNotification n) async {
    if (n.read) return;
    try {
      await context.read<AppState>().api.markNotificationRead(n.id);
      _refresh();
    } catch (_) {/* best effort */}
  }

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.of(context).padding.top + kToolbarHeight;
    final bottom = MediaQuery.of(context).padding.bottom + 64;

    return AsyncView<(List<AppNotification>, int)>(
      future: _future,
      onRefresh: _refresh,
      isEmpty: (data) => data.$1.isEmpty,
      emptyMessage: 'No alerts.\nWe will let you know when something happens.',
      builder: (context, data) {
        final items = data.$1;
        return ListView.builder(
          padding: EdgeInsets.fromLTRB(16, top + 8, 16, bottom + 16),
          itemCount: items.length,
          itemBuilder: (context, i) => _NotificationCard(
            notif: items[i],
            onTap: () => _markRead(items[i]),
          ),
        );
      },
    );
  }
}

// ── Notification card — transmission aesthetic ────────────────────────────────

class _NotificationCard extends StatelessWidget {
  const _NotificationCard({required this.notif, required this.onTap});
  final AppNotification notif;
  final VoidCallback onTap;

  String _when() {
    final dt = DateTime.tryParse(notif.sentAt);
    if (dt == null) return '';
    return DateFormat.yMMMd().add_jm().format(dt.toLocal());
  }

  @override
  Widget build(BuildContext context) {
    final unread = !notif.read;
    final accentColor =
        unread ? BSTheme.accent : BSTheme.ink3.withValues(alpha: 0.5);

    return GestureDetector(
      onTap: onTap,
      child: Container(
        margin: const EdgeInsets.only(bottom: 10),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(20),
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [
              (unread ? BSTheme.accent : const Color(0xFFA0B9FF))
                  .withValues(alpha: unread ? 0.12 : 0.06),
              const Color(0x12A0B9FF),
              const Color(0x08060E1E),
            ],
            stops: const [0.0, 0.45, 1.0],
          ),
          border: Border.all(
            color: unread
                ? BSTheme.accent.withValues(alpha: 0.28)
                : BSTheme.glassBorder,
          ),
          boxShadow: unread
              ? [
                  BoxShadow(
                    color: BSTheme.accent.withValues(alpha: 0.12),
                    blurRadius: 22,
                    spreadRadius: -8,
                    offset: const Offset(0, 8),
                  ),
                ]
              : null,
        ),
        child: IntrinsicHeight(
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              // Left accent bar — glows on unread
              Container(
                width: 3,
                decoration: BoxDecoration(
                  borderRadius: const BorderRadius.only(
                    topLeft: Radius.circular(20),
                    bottomLeft: Radius.circular(20),
                  ),
                  color: accentColor,
                  boxShadow: unread
                      ? [
                          BoxShadow(
                            color: BSTheme.accent.withValues(alpha: 0.5),
                            blurRadius: 8,
                            spreadRadius: 1,
                          ),
                        ]
                      : null,
                ),
              ),
              // Content
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(14, 14, 14, 14),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      // Header row
                      Row(
                        children: [
                          Icon(
                            unread
                                ? Icons.radio_outlined
                                : Icons.notifications_none,
                            size: 13,
                            color: accentColor,
                          ),
                          const SizedBox(width: 5),
                          Text(
                            unread ? 'INCOMING' : 'RECEIVED',
                            style: TextStyle(
                              fontFamily: 'Geist',
                              fontSize: 9,
                              fontWeight: FontWeight.w600,
                              letterSpacing: 2.5,
                              color: accentColor,
                            ),
                          ),
                          const Spacer(),
                          Text(
                            _when(),
                            style: const TextStyle(
                              fontFamily: 'Geist',
                              fontSize: 10,
                              color: BSTheme.ink3,
                            ),
                          ),
                          if (unread) ...[
                            const SizedBox(width: 8),
                            Semantics(
                              label: 'Unread',
                              child: Container(
                                width: 7,
                                height: 7,
                                decoration: BoxDecoration(
                                  shape: BoxShape.circle,
                                  color: BSTheme.accent,
                                  boxShadow: [
                                    BoxShadow(
                                      color: BSTheme.accent
                                          .withValues(alpha: 0.7),
                                      blurRadius: 6,
                                      spreadRadius: 1,
                                    ),
                                  ],
                                ),
                              ),
                            ),
                          ],
                        ],
                      ),
                      const SizedBox(height: 8),
                      // Title
                      Text(
                        notif.title,
                        style: TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 14,
                          fontWeight:
                              unread ? FontWeight.w600 : FontWeight.w400,
                          letterSpacing: -0.2,
                          color: unread ? BSTheme.ink : BSTheme.ink2,
                          height: 1.4,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
