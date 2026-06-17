import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'api/auth_store.dart';
import 'firebase_options.dart';
import 'screens/home_screen.dart';
import 'screens/login_screen.dart';
import 'services/push_service.dart';
import 'state/app_state.dart';
import 'theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Firebase initialises with values from firebase_options.dart.
  // If the file still has TODO placeholders the init throws — caught here so
  // the app runs normally without push notifications until Firebase is set up.
  try {
    await Firebase.initializeApp(
      options: DefaultFirebaseOptions.currentPlatform,
    );
  } catch (_) {
    // Firebase not yet configured — push notifications will be unavailable.
  }

  final appState = AppState(AuthStore());
  await appState.bootstrap();

  runApp(
    ChangeNotifierProvider.value(value: appState, child: const BoundlessSkiesApp()),
  );
}

class BoundlessSkiesApp extends StatefulWidget {
  const BoundlessSkiesApp({super.key});

  @override
  State<BoundlessSkiesApp> createState() => _BoundlessSkiesAppState();
}

class _BoundlessSkiesAppState extends State<BoundlessSkiesApp> {
  @override
  void initState() {
    super.initState();
    // Defer until the first frame so context.read can resolve the Provider.
    WidgetsBinding.instance.addPostFrameCallback((_) => _initPush());
  }

  Future<void> _initPush() async {
    if (!mounted) return;
    final state = context.read<AppState>();
    await PushService.initialize(
      onToken: (token) =>
          state.api.setNotificationPrefs(push: true, pushToken: token),
      onNotificationTap: (data) {
        // A night_summary notification tap navigates the user to Alerts tab.
        // The AppState exposes a setter that HomeScreen listens to.
        if (data['type'] == 'night_summary') {
          state.pendingTab = 3; // Alerts tab index
        }
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Boundless Skies',
      debugShowCheckedModeBanner: false,
      theme: BSTheme.dark(),
      home: const _AuthGate(),
    );
  }
}

/// Routes between login and the home shell based on auth status.
class _AuthGate extends StatelessWidget {
  const _AuthGate();

  @override
  Widget build(BuildContext context) {
    final status = context.select<AppState, AuthStatus>((s) => s.status);
    switch (status) {
      case AuthStatus.unknown:
        return const Scaffold(body: Center(child: CircularProgressIndicator()));
      case AuthStatus.signedOut:
        return const LoginScreen();
      case AuthStatus.signedIn:
        return const HomeScreen();
    }
  }
}
