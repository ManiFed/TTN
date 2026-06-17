import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';

/// Background message handler — must be a top-level function.
/// Firebase invokes this when the app is terminated and a data message arrives.
@pragma('vm:entry-point')
Future<void> _onBackgroundMessage(RemoteMessage message) async {
  debugPrint('FCM background: ${message.messageId}');
}

/// Thin wrapper over firebase_messaging.
///
/// Initialisation is fully guarded: if Firebase was not configured (i.e.
/// firebase_options.dart still has TODO placeholders) every method silently
/// returns null / false so the rest of the app is unaffected.
///
/// Usage:
///   final token = await PushService.initialize(
///     onToken: (t) => api.setNotificationPrefs(pushToken: t, push: true),
///     onNotificationTap: (payload) { /* navigate */ },
///   );
class PushService {
  static bool _ready = false;

  /// Initialises FCM, requests permission, registers the token with the cloud,
  /// and wires up tap-to-open navigation. Returns the FCM token on success, or
  /// null if Firebase is not configured or permission is denied.
  static Future<String?> initialize({
    required Future<void> Function(String token) onToken,
    required void Function(Map<String, dynamic> data) onNotificationTap,
  }) async {
    try {
      final messaging = FirebaseMessaging.instance;

      // Request display permission (required on iOS / Android 13+).
      final settings = await messaging.requestPermission(
        alert: true,
        badge: true,
        sound: true,
        announcement: false,
        carPlay: false,
        criticalAlert: false,
        provisional: false,
      );

      if (settings.authorizationStatus != AuthorizationStatus.authorized &&
          settings.authorizationStatus != AuthorizationStatus.provisional) {
        debugPrint('FCM permission not granted: ${settings.authorizationStatus}');
        return null;
      }

      FirebaseMessaging.onBackgroundMessage(_onBackgroundMessage);

      // Notification tapped while app was terminated.
      final initial = await messaging.getInitialMessage();
      if (initial != null) {
        onNotificationTap(initial.data);
      }

      // Notification tapped while app was backgrounded.
      FirebaseMessaging.onMessageOpenedApp.listen((msg) {
        onNotificationTap(msg.data);
      });

      // Foreground messages — the system bar notification is not shown
      // automatically on iOS; we rely on in-app polling instead and just
      // log here to avoid duplicating alerts.
      FirebaseMessaging.onMessage.listen((msg) {
        debugPrint(
            'FCM foreground: ${msg.notification?.title} — '
            'member will see it in the Alerts tab');
      });

      // Fetch and register the current token.
      final token = await messaging.getToken();
      if (token != null) {
        await onToken(token);
        debugPrint('FCM token registered: ${token.substring(0, 12)}…');
      }

      // Re-register if FCM rotates the token.
      messaging.onTokenRefresh.listen(onToken);

      _ready = true;
      return token;
    } catch (e) {
      // Firebase not configured or platform limitation — app works without push.
      debugPrint('Push notifications unavailable: $e');
      return null;
    }
  }

  static bool get isReady => _ready;
}
