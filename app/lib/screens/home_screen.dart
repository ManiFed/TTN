import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../state/app_state.dart';
import 'dashboard_tab.dart';
import 'me_screen.dart';
import 'nodes_tab.dart';
import 'notifications_tab.dart';
import 'observations_tab.dart';

/// The signed-in shell: a NavigationBar over the four member tabs.
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
    // Consume any pending deep-link from a notification tap.
    final state = context.read<AppState>();
    if (state.pendingTab != null) {
      final tab = state.pendingTab!;
      state.pendingTab = null;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) setState(() => _index = tab);
      });
    }
  }

  static const _titles = ['Tonight', 'Telescopes', 'Observations', 'Alerts'];

  @override
  Widget build(BuildContext context) {
    final name = context.select<AppState, String>(
      (s) => s.member?.displayName ?? '',
    );

    final pages = const [
      DashboardTab(),
      NodesTab(),
      ObservationsTab(),
      NotificationsTab(),
    ];

    return Scaffold(
      appBar: AppBar(
        title: Text(_titles[_index]),
        actions: [
          PopupMenuButton<String>(
            icon: const Icon(Icons.account_circle_outlined, size: 30),
            tooltip: 'Account',
            onSelected: (v) {
              if (v == 'me') {
                Navigator.of(context).push(
                  MaterialPageRoute<void>(
                      builder: (_) => const MeScreen()),
                );
              } else if (v == 'signout') {
                context.read<AppState>().signOut();
              }
            },
            itemBuilder: (context) => [
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
        ],
      ),
      body: IndexedStack(index: _index, children: pages),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        height: 72,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.nightlight_outlined),
            selectedIcon: Icon(Icons.nightlight),
            label: 'Tonight',
          ),
          NavigationDestination(
            icon: Icon(Icons.satellite_alt_outlined),
            selectedIcon: Icon(Icons.satellite_alt),
            label: 'Telescopes',
          ),
          NavigationDestination(
            icon: Icon(Icons.show_chart_outlined),
            selectedIcon: Icon(Icons.show_chart),
            label: 'Observations',
          ),
          NavigationDestination(
            icon: Icon(Icons.notifications_outlined),
            selectedIcon: Icon(Icons.notifications),
            label: 'Alerts',
          ),
        ],
      ),
    );
  }
}
