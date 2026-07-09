import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'api/auth_store.dart';
import 'screens/home_screen.dart';
import 'screens/login_screen.dart';
import 'state/app_state.dart';
import 'theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final appState = AppState(AuthStore());
  await appState.bootstrap();

  runApp(
    ChangeNotifierProvider.value(value: appState, child: const TelescopeNetApp()),
  );
}

class TelescopeNetApp extends StatefulWidget {
  const TelescopeNetApp({super.key});

  @override
  State<TelescopeNetApp> createState() => _TelescopeNetAppState();
}

class _TelescopeNetAppState extends State<TelescopeNetApp>
    with WidgetsBindingObserver {
  Timer? _notificationPoller;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _notificationPoller = Timer.periodic(
      const Duration(minutes: 2),
      (_) => context.read<AppState>().refreshUnreadNotifications(),
    );
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && mounted) {
      context.read<AppState>().refreshUnreadNotifications();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _notificationPoller?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'The Telescope Net',
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
