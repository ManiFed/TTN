import 'package:flutter/material.dart';

/// A FutureBuilder with accessible, consistent loading / error / empty states.
/// Pull-to-refresh is wired in so screens stay one-liners.
class AsyncView<T> extends StatelessWidget {
  const AsyncView({
    super.key,
    required this.future,
    required this.onRefresh,
    required this.builder,
    this.isEmpty,
    this.emptyMessage = 'Nothing here yet.',
  });

  final Future<T> future;
  final Future<void> Function() onRefresh;
  final Widget Function(BuildContext, T) builder;
  final bool Function(T)? isEmpty;
  final String emptyMessage;

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<T>(
      future: future,
      builder: (context, snap) {
        if (snap.connectionState == ConnectionState.waiting) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snap.hasError) {
          return _Message(
            icon: Icons.cloud_off,
            title: 'Something went wrong',
            detail: '${snap.error}',
            onRetry: onRefresh,
          );
        }
        final data = snap.data as T;
        if (isEmpty?.call(data) ?? false) {
          return RefreshIndicator(
            onRefresh: onRefresh,
            child: ListView(
              children: [
                const SizedBox(height: 120),
                _Message(icon: Icons.nights_stay, title: emptyMessage),
              ],
            ),
          );
        }
        return RefreshIndicator(onRefresh: onRefresh, child: builder(context, data));
      },
    );
  }
}

class _Message extends StatelessWidget {
  const _Message({
    required this.icon,
    required this.title,
    this.detail,
    this.onRetry,
  });

  final IconData icon;
  final String title;
  final String? detail;
  final Future<void> Function()? onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 56, semanticLabel: title),
            const SizedBox(height: 16),
            Text(title,
                style: Theme.of(context).textTheme.titleLarge,
                textAlign: TextAlign.center),
            if (detail != null) ...[
              const SizedBox(height: 8),
              Text(detail!,
                  style: Theme.of(context).textTheme.bodyMedium,
                  textAlign: TextAlign.center),
            ],
            if (onRetry != null) ...[
              const SizedBox(height: 24),
              ElevatedButton.icon(
                onPressed: onRetry,
                icon: const Icon(Icons.refresh),
                label: const Text('Try again'),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
