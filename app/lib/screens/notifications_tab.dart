import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../models/models.dart';
import '../state/app_state.dart';
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
    return AsyncView<(List<AppNotification>, int)>(
      future: _future,
      onRefresh: _refresh,
      isEmpty: (data) => data.$1.isEmpty,
      emptyMessage: 'No alerts.\nWe will let you know when something happens.',
      builder: (context, data) {
        final items = data.$1;
        return ListView.builder(
          padding: const EdgeInsets.all(16),
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
    return Card(
      child: ListTile(
        onTap: onTap,
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        leading: Icon(
          unread ? Icons.circle_notifications : Icons.notifications_none,
          size: 30,
          color: unread
              ? Theme.of(context).colorScheme.primary
              : Theme.of(context).disabledColor,
        ),
        title: Text(
          notif.title,
          style: Theme.of(context).textTheme.titleMedium?.copyWith(
                fontWeight: unread ? FontWeight.w700 : FontWeight.w400,
              ),
        ),
        subtitle: Text(_when(), style: Theme.of(context).textTheme.bodySmall),
        trailing: unread
            ? Semantics(
                label: 'Unread',
                child: Container(
                  width: 12,
                  height: 12,
                  decoration: BoxDecoration(
                    color: Theme.of(context).colorScheme.secondary,
                    shape: BoxShape.circle,
                  ),
                ),
              )
            : null,
      ),
    );
  }
}
