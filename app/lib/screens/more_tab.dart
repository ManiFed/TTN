import 'package:flutter/material.dart';

import '../theme.dart';
import 'help_tab.dart';
import 'suggest_program_screen.dart';

/// Secondary destinations gated behind the More tab.
class MoreTab extends StatelessWidget {
  const MoreTab({super.key});

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.of(context).padding.top + kToolbarHeight;
    final bottom = MediaQuery.of(context).padding.bottom + 64;

    return ListView(
      padding: EdgeInsets.fromLTRB(16, top + 16, 16, bottom + 16),
      children: [
        const Text(
          'MORE',
          style: TextStyle(
            fontFamily: 'Geist',
            fontSize: 11,
            fontWeight: FontWeight.w600,
            letterSpacing: 1.0,
            color: BSTheme.ink3,
          ),
        ),
        const SizedBox(height: 8),
        const Text(
          'Support, feedback, and program ideas.',
          style: TextStyle(
            fontFamily: 'Geist',
            fontSize: 14,
            color: BSTheme.ink2,
          ),
        ),
        const SizedBox(height: 20),
        _MoreTile(
          icon: Icons.support_agent_outlined,
          title: 'Help',
          subtitle: 'Contact support, AI assistant, and node diagnostics',
          onTap: () => Navigator.push(
            context,
            MaterialPageRoute<void>(builder: (_) => const _HelpPage()),
          ),
        ),
        const SizedBox(height: 10),
        _MoreTile(
          icon: Icons.science_outlined,
          title: 'Suggest a science program',
          subtitle: 'Propose targets or observing campaigns for the network',
          onTap: () => Navigator.push(
            context,
            MaterialPageRoute<void>(
              builder: (_) => const SuggestProgramScreen(),
            ),
          ),
        ),
      ],
    );
  }
}

class _HelpPage extends StatelessWidget {
  const _HelpPage();

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: BSTheme.night,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        elevation: 0,
        title: const Text(
          'Help',
          style: TextStyle(
            fontFamily: 'Geist',
            fontSize: 16,
            fontWeight: FontWeight.w800,
            color: BSTheme.ink,
          ),
        ),
        iconTheme: const IconThemeData(color: BSTheme.ink2),
      ),
      body: const HelpTab(),
    );
  }
}

class _MoreTile extends StatelessWidget {
  const _MoreTile({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.onTap,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: BSTheme.surface.withValues(alpha: 0.88),
      borderRadius: BorderRadius.circular(12),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Container(
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: BSTheme.glassBorder),
          ),
          child: Row(
            children: [
              Container(
                width: 44,
                height: 44,
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(10),
                  color: BSTheme.accent.withValues(alpha: 0.10),
                  border: Border.all(color: BSTheme.accent.withValues(alpha: 0.28)),
                ),
                child: Icon(icon, color: BSTheme.accent, size: 22),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: const TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 15,
                        fontWeight: FontWeight.w800,
                        color: BSTheme.ink,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      subtitle,
                      style: const TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 13,
                        color: BSTheme.ink2,
                        height: 1.4,
                      ),
                    ),
                  ],
                ),
              ),
              const Icon(Icons.chevron_right, color: BSTheme.ink3, size: 20),
            ],
          ),
        ),
      ),
    );
  }
}