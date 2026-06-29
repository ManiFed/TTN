import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api/api_client.dart';
import '../models/models.dart';
import '../models/node_status.dart';
import '../state/app_state.dart';
import '../theme.dart';
import '../widgets/async_view.dart';

/// Help tab: contact links, AI assistant chat, and node diagnostics.
class HelpTab extends StatefulWidget {
  const HelpTab({super.key});

  @override
  State<HelpTab> createState() => _HelpTabState();
}

class _HelpTabState extends State<HelpTab> {
  late Future<_HelpData> _future;
  final _composer = TextEditingController();
  final _scroll = ScrollController();

  List<HelpChatMessage> _messages = [];
  int _messagesRemaining = 0;
  int _weeklyLimit = 5;
  bool _sending = false;
  String? _sendError;
  bool _diagExpanded = false;
  bool _sessionHydrated = false;

  @override
  void initState() {
    super.initState();
    _future = _loadAndHydrate();
  }

  Future<_HelpData> _loadAndHydrate({bool force = false}) async {
    final data = await _load();
    if (mounted && (!_sessionHydrated || force)) {
      setState(() {
        _syncFromSession(data.session);
        if (!data.chatAvailable) _diagExpanded = true;
        _sessionHydrated = true;
      });
    }
    return data;
  }

  @override
  void dispose() {
    _composer.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<_HelpData> _load() async {
    final api = context.read<AppState>().api;
    HelpSession session;
    var chatAvailable = true;
    try {
      session = await api.helpSession();
    } on ApiException catch (e) {
      if (e.statusCode != 404) rethrow;
      session = _fallbackSession();
      chatAvailable = false;
    }
    final nodes = await api.nodes().catchError((_) => <Node>[]);
    final timeline =
        await api.timeline().catchError((_) => <TimelineItem>[]);
    return _HelpData(
      session: session,
      nodes: nodes,
      timeline: timeline,
      chatAvailable: chatAvailable,
    );
  }

  static HelpSession _fallbackSession() => const HelpSession(
        contact: HelpContact(
          email: 'info@thetelescope.net',
          appUrl: 'https://app.thetelescope.net',
          docsUrl: 'https://thetelescope.net',
          github: 'https://github.com/telescopenet',
        ),
        weeklyLimit: 5,
        messagesUsed: 0,
        messagesRemaining: 0,
        messages: [],
      );

  Future<void> _refresh() async {
    setState(() => _future = _loadAndHydrate(force: true));
    await _future;
  }

  void _syncFromSession(HelpSession session) {
    _messages = List.of(session.messages);
    _messagesRemaining = session.messagesRemaining;
    _weeklyLimit = session.weeklyLimit;
  }

  Future<void> _send(_HelpData data) async {
    final text = _composer.text.trim();
    if (text.isEmpty || _sending || _messagesRemaining <= 0) return;

    setState(() {
      _sending = true;
      _sendError = null;
    });

    final api = context.read<AppState>().api;
    final nodeId = data.nodes.isNotEmpty ? data.nodes.first.nodeId : null;

    try {
      final resp = await api.helpChat(text, nodeId: nodeId);
      if (!mounted) return;
      setState(() {
        _messages = [
          ..._messages,
          HelpChatMessage(
            id: -_messages.length,
            role: 'user',
            content: text,
            createdAt: '',
          ),
          HelpChatMessage(
            id: -_messages.length - 1,
            role: 'assistant',
            content: resp.reply,
            createdAt: '',
            configPatch: resp.configPatch,
          ),
        ];
        _messagesRemaining = resp.messagesRemaining;
        _weeklyLimit = resp.weeklyLimit;
        _composer.clear();
        _sending = false;
      });
      _scrollToEnd();
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _sendError = e.message;
        _sending = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _sendError = '$e';
        _sending = false;
      });
    }
  }

  void _scrollToEnd() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.animateTo(
        _scroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 250),
        curve: Curves.easeOut,
      );
    });
  }

  Future<void> _openUrl(String url) async {
    final uri = Uri.tryParse(url);
    if (uri == null) return;
    await launchUrl(uri, mode: LaunchMode.externalApplication);
  }

  @override
  Widget build(BuildContext context) {
    final top = MediaQuery.of(context).padding.top + kToolbarHeight;
    final bottom = MediaQuery.of(context).padding.bottom + 64;

    return AsyncView<_HelpData>(
      future: _future,
      onRefresh: _refresh,
      builder: (context, data) {
        return Column(
          children: [
            Expanded(
              child: ListView(
                controller: _scroll,
                padding: EdgeInsets.fromLTRB(16, top + 12, 16, 12),
                children: [
                  const _SectionTitle(
                    label: 'SUPPORT',
                    title: 'Help',
                    subtitle:
                        'Contact us, chat with the assistant, or inspect live node telemetry.',
                  ),
                  const SizedBox(height: 16),
                  _ContactCard(
                    contact: data.session.contact,
                    onOpen: _openUrl,
                  ),
                  if (!data.chatAvailable) ...[
                    const SizedBox(height: 14),
                    const _ChatUnavailableBanner(),
                  ] else ...[
                    const SizedBox(height: 14),
                    _QuotaBar(
                      remaining: _messagesRemaining,
                      limit: _weeklyLimit,
                    ),
                    const SizedBox(height: 16),
                    _ChatSection(
                      messages: _messages,
                      sending: _sending,
                    ),
                  ],
                  const SizedBox(height: 16),
                  _DiagnosticsPanel(
                    expanded: _diagExpanded,
                    onToggle: () =>
                        setState(() => _diagExpanded = !_diagExpanded),
                    nodes: data.nodes,
                    timeline: data.timeline,
                  ),
                  SizedBox(height: bottom + 8),
                ],
              ),
            ),
            if (data.chatAvailable)
              _ChatComposer(
                controller: _composer,
                remaining: _messagesRemaining,
                sending: _sending,
                error: _sendError,
                onSend: () => _send(data),
                bottomPadding: bottom,
              ),
          ],
        );
      },
    );
  }
}

class _HelpData {
  const _HelpData({
    required this.session,
    required this.nodes,
    required this.timeline,
    this.chatAvailable = true,
  });

  final HelpSession session;
  final List<Node> nodes;
  final List<TimelineItem> timeline;
  final bool chatAvailable;
}

class _ContactCard extends StatelessWidget {
  const _ContactCard({required this.contact, required this.onOpen});

  final HelpContact contact;
  final Future<void> Function(String url) onOpen;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: BSTheme.surface.withValues(alpha: 0.9),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: BSTheme.glassBorder),
      ),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 14, 14, 10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              'CONTACT',
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 10,
                fontWeight: FontWeight.w800,
                letterSpacing: 1.0,
                color: BSTheme.ink3,
              ),
            ),
            const SizedBox(height: 10),
            _ContactLink(
              icon: Icons.mail_outline,
              label: contact.email,
              onTap: () => onOpen('mailto:${contact.email}'),
            ),
            if (contact.appUrl.isNotEmpty)
              _ContactLink(
                icon: Icons.public_outlined,
                label: 'App',
                onTap: () => onOpen(contact.appUrl),
              ),
            if (contact.docsUrl.isNotEmpty)
              _ContactLink(
                icon: Icons.menu_book_outlined,
                label: 'Documentation',
                onTap: () => onOpen(contact.docsUrl),
              ),
            if (contact.github.isNotEmpty)
              _ContactLink(
                icon: Icons.code_outlined,
                label: 'GitHub',
                onTap: () => onOpen(contact.github),
              ),
          ],
        ),
      ),
    );
  }
}

class _ContactLink extends StatelessWidget {
  const _ContactLink({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(4),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 6),
          child: Row(
            children: [
              Icon(icon, size: 16, color: BSTheme.accent),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  label,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                    color: BSTheme.accent,
                    decoration: TextDecoration.underline,
                    decorationColor: BSTheme.accent,
                  ),
                ),
              ),
              const Icon(Icons.open_in_new, size: 14, color: BSTheme.ink3),
            ],
          ),
        ),
      ),
    );
  }
}

class _ChatUnavailableBanner extends StatelessWidget {
  const _ChatUnavailableBanner();

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: BSTheme.warm.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: BSTheme.warm.withValues(alpha: 0.25)),
      ),
      child: const Padding(
        padding: EdgeInsets.all(12),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.info_outline, size: 18, color: BSTheme.warm),
            SizedBox(width: 10),
            Expanded(
              child: Text(
                'The assistant is not live on the server yet. '
                'Contact links and node diagnostics still work — '
                'email us if you need help right away.',
                style: TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 12,
                  color: BSTheme.ink2,
                  height: 1.45,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _QuotaBar extends StatelessWidget {
  const _QuotaBar({required this.remaining, required this.limit});

  final int remaining;
  final int limit;

  @override
  Widget build(BuildContext context) {
    final used = (limit - remaining).clamp(0, limit);
    final ratio = limit > 0 ? used / limit : 0.0;
    final color = remaining == 0
        ? BSTheme.danger
        : remaining <= 1
            ? BSTheme.warm
            : BSTheme.accent;

    return DecoratedBox(
      decoration: BoxDecoration(
        color: BSTheme.surface.withValues(alpha: 0.7),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: BSTheme.glassBorder),
      ),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.chat_bubble_outline, size: 16, color: color),
                const SizedBox(width: 8),
                Text(
                  remaining > 0
                      ? '$remaining of $limit assistant messages left this week'
                      : 'Weekly assistant limit reached ($limit messages)',
                  style: TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    color: color,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            ClipRRect(
              borderRadius: BorderRadius.circular(2),
              child: LinearProgressIndicator(
                value: ratio,
                minHeight: 4,
                backgroundColor: BSTheme.glassBorder,
                color: color,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ChatSection extends StatelessWidget {
  const _ChatSection({required this.messages, required this.sending});

  final List<HelpChatMessage> messages;
  final bool sending;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: BSTheme.surface.withValues(alpha: 0.9),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: BSTheme.glassBorder),
      ),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 14, 14, 14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              'ASSISTANT',
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 10,
                fontWeight: FontWeight.w800,
                letterSpacing: 1.0,
                color: BSTheme.ink3,
              ),
            ),
            const SizedBox(height: 10),
            if (messages.isEmpty)
              const Text(
                'Ask about observing status, auto-run settings, safety blocks, '
                'or anything else about your telescope node.',
                style: TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 13,
                  color: BSTheme.ink3,
                  height: 1.45,
                ),
              )
            else
              ...messages.map((m) => _ChatBubble(message: m)),
            if (sending)
              const Padding(
                padding: EdgeInsets.only(top: 10),
                child: Row(
                  children: [
                    SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                    SizedBox(width: 10),
                    Text(
                      'Thinking…',
                      style: TextStyle(
                        fontFamily: 'Geist',
                        fontSize: 12,
                        color: BSTheme.ink3,
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _ChatBubble extends StatelessWidget {
  const _ChatBubble({required this.message});

  final HelpChatMessage message;

  @override
  Widget build(BuildContext context) {
    final isUser = message.isUser;
    final bg = isUser
        ? BSTheme.accent.withValues(alpha: 0.14)
        : BSTheme.ink.withValues(alpha: 0.05);
    final align = isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start;

    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: align,
        children: [
          Container(
            constraints: const BoxConstraints(maxWidth: 520),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            decoration: BoxDecoration(
              color: bg,
              borderRadius: BorderRadius.circular(6),
              border: Border.all(
                color: isUser
                    ? BSTheme.accent.withValues(alpha: 0.25)
                    : BSTheme.glassBorder,
              ),
            ),
            child: Text(
              message.content,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 13,
                color: BSTheme.ink2,
                height: 1.45,
              ),
            ),
          ),
          if (!isUser &&
              message.configPatch != null &&
              message.configPatch!.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: _PatchNotice(patch: message.configPatch!),
            ),
        ],
      ),
    );
  }
}

class _PatchNotice extends StatelessWidget {
  const _PatchNotice({required this.patch});

  final Map<String, dynamic> patch;

  @override
  Widget build(BuildContext context) {
    final encoder = const JsonEncoder.withIndent('  ');
    final preview = encoder.convert(patch);

    return Container(
      constraints: const BoxConstraints(maxWidth: 520),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: BSTheme.success.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: BSTheme.success.withValues(alpha: 0.25)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Row(
            children: [
              Icon(Icons.settings_suggest_outlined,
                  size: 16, color: BSTheme.success),
              SizedBox(width: 8),
              Text(
                'Config change queued',
                style: TextStyle(
                  fontFamily: 'Geist',
                  fontSize: 12,
                  fontWeight: FontWeight.w800,
                  color: BSTheme.success,
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          const Text(
            'Your node agent will apply this on its next cloud poll.',
            style: TextStyle(
              fontFamily: 'Geist',
              fontSize: 11,
              color: BSTheme.ink3,
              height: 1.4,
            ),
          ),
          const SizedBox(height: 6),
          SelectableText(
            preview,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 10,
              color: BSTheme.ink3,
              height: 1.4,
            ),
          ),
        ],
      ),
    );
  }
}

class _ChatComposer extends StatelessWidget {
  const _ChatComposer({
    required this.controller,
    required this.remaining,
    required this.sending,
    required this.onSend,
    required this.bottomPadding,
    this.error,
  });

  final TextEditingController controller;
  final int remaining;
  final bool sending;
  final VoidCallback onSend;
  final double bottomPadding;
  final String? error;

  @override
  Widget build(BuildContext context) {
    final disabled = sending || remaining <= 0;

    return Container(
      padding: EdgeInsets.fromLTRB(16, 10, 16, bottomPadding),
      decoration: const BoxDecoration(
        color: Color(0xF2030404),
        border: Border(top: BorderSide(color: BSTheme.glassBorder)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (error != null) ...[
            Text(
              error!,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                color: BSTheme.danger,
              ),
            ),
            const SizedBox(height: 8),
          ],
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: TextField(
                  controller: controller,
                  enabled: !disabled,
                  minLines: 1,
                  maxLines: 4,
                  style: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 14,
                    color: BSTheme.ink,
                  ),
                  decoration: InputDecoration(
                    hintText: remaining > 0
                        ? 'Ask the assistant…'
                        : 'Weekly limit reached',
                    hintStyle: const TextStyle(color: BSTheme.ink3),
                    filled: true,
                    fillColor: BSTheme.surface,
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(6),
                      borderSide: const BorderSide(color: BSTheme.glassBorder),
                    ),
                    enabledBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(6),
                      borderSide: const BorderSide(color: BSTheme.glassBorder),
                    ),
                    contentPadding: const EdgeInsets.symmetric(
                      horizontal: 12,
                      vertical: 12,
                    ),
                  ),
                  onSubmitted: disabled ? null : (_) => onSend(),
                ),
              ),
              const SizedBox(width: 10),
              FilledButton(
                onPressed: disabled ? null : onSend,
                style: FilledButton.styleFrom(
                  backgroundColor: BSTheme.btnPrimary,
                  foregroundColor: BSTheme.btnPrimaryFg,
                  padding: const EdgeInsets.symmetric(
                    horizontal: 16,
                    vertical: 14,
                  ),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(6),
                  ),
                ),
                child: sending
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(
                          strokeWidth: 2,
                          color: BSTheme.night,
                        ),
                      )
                    : const Icon(Icons.send_rounded, size: 18),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _DiagnosticsPanel extends StatelessWidget {
  const _DiagnosticsPanel({
    required this.expanded,
    required this.onToggle,
    required this.nodes,
    required this.timeline,
  });

  final bool expanded;
  final VoidCallback onToggle;
  final List<Node> nodes;
  final List<TimelineItem> timeline;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: BSTheme.surface.withValues(alpha: 0.9),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: BSTheme.glassBorder),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          InkWell(
            onTap: onToggle,
            borderRadius: BorderRadius.circular(6),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
              child: Row(
                children: [
                  Icon(
                    expanded ? Icons.expand_less : Icons.expand_more,
                    size: 20,
                    color: BSTheme.ink3,
                  ),
                  const SizedBox(width: 10),
                  const Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'NODE DIAGNOSTICS',
                          style: TextStyle(
                            fontFamily: 'Geist',
                            fontSize: 10,
                            fontWeight: FontWeight.w800,
                            letterSpacing: 1.0,
                            color: BSTheme.ink3,
                          ),
                        ),
                        SizedBox(height: 4),
                        Text(
                          'Live telemetry from last cloud heartbeat',
                          style: TextStyle(
                            fontFamily: 'Geist',
                            fontSize: 13,
                            fontWeight: FontWeight.w700,
                            color: BSTheme.ink2,
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
          if (expanded) ...[
            const Divider(height: 1, color: BSTheme.glassBorder),
            if (nodes.isEmpty)
              const Padding(
                padding: EdgeInsets.all(14),
                child: Text(
                  'Connect a telescope to see diagnostics.',
                  style: TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 13,
                    color: BSTheme.ink3,
                  ),
                ),
              )
            else
              ...nodes.map(
                (node) => _NodeDiagnosticsCard(
                  node: node,
                  timeline: timeline
                      .where((t) => t.nodeId == node.nodeId)
                      .toList(),
                ),
              ),
          ],
        ],
      ),
    );
  }
}

class _NodeDiagnosticsCard extends StatelessWidget {
  const _NodeDiagnosticsCard({required this.node, required this.timeline});

  final Node node;
  final List<TimelineItem> timeline;

  @override
  Widget build(BuildContext context) {
    final c = node.conditions;
    final live = primaryNodeStatus(
      node: node,
      planCount: timeline.length,
      activePlanTarget: timeline.isNotEmpty ? timeline.first.target : null,
    );
    return Padding(
      padding: const EdgeInsets.fromLTRB(0, 0, 0, 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 14, 14, 10),
            child: Row(
              children: [
                Icon(live.icon, color: live.color, size: 20),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        node.label,
                        style: const TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 16,
                          fontWeight: FontWeight.w800,
                          color: BSTheme.ink,
                        ),
                      ),
                      Text(
                        live.headline,
                        style: TextStyle(
                          fontFamily: 'Geist',
                          fontSize: 13,
                          fontWeight: FontWeight.w700,
                          color: live.color,
                        ),
                      ),
                    ],
                  ),
                ),
                _Pill(
                  label: node.online ? 'ONLINE' : 'OFFLINE',
                  color: node.online ? BSTheme.success : BSTheme.danger,
                ),
              ],
            ),
          ),
          const Divider(height: 1, color: BSTheme.glassBorder),
          _DiagCard(
            title: 'Connection',
            children: [
              _DiagRow(label: 'Node ID', value: node.nodeId),
              _DiagRow(
                label: 'Last heartbeat',
                value: heartbeatAgeLabel(node.lastHeartbeat),
              ),
              _DiagRow(label: 'Account status', value: _statusLabel(node.status)),
              _DiagRow(
                label: 'Portable',
                value: node.portable ? 'Yes' : 'Fixed',
              ),
              if (node.isOnVacation && node.vacationUntil.isNotEmpty)
                _DiagRow(label: 'Vacation until', value: node.vacationUntil),
            ],
          ),
          _DiagCard(
            title: 'Hardware',
            children: [
              _DiagRow(
                label: 'Model',
                value: node.telescopeModel.isEmpty ? '—' : node.telescopeModel,
              ),
              if (node.telescopeName.isNotEmpty)
                _DiagRow(label: 'Device name', value: node.telescopeName),
              _DiagRow(label: 'Telescope link', value: _tri(c.telescopeConnected)),
              _DiagRow(label: 'Camera link', value: _tri(c.cameraConnected)),
              _DiagRow(label: 'Scope heartbeat', value: _tri(c.heartbeatOk)),
            ],
          ),
          _DiagCard(
            title: 'Safety & sky',
            children: [
              _DiagRow(label: 'Safe to observe', value: _tri(c.safe)),
              if (c.reason.isNotEmpty)
                _DiagRow(label: 'Safety reason', value: c.reason),
              if (c.sunElevation != null)
                _DiagRow(
                  label: 'Sun elevation',
                  value: '${c.sunElevation!.toStringAsFixed(1)}°',
                ),
              if (c.dawnThreshold != null)
                _DiagRow(
                  label: 'Night threshold',
                  value: '${c.dawnThreshold!.toStringAsFixed(0)}°',
                ),
            ],
          ),
          _DiagCard(
            title: 'Schedule',
            children: [
              _DiagRow(
                label: 'Running now',
                value: c.scheduleRunning ? 'Yes' : 'No',
              ),
              if (c.scheduleTarget.isNotEmpty)
                _DiagRow(label: 'Current target', value: c.scheduleTarget),
              if (c.schedulePhase.isNotEmpty)
                _DiagRow(label: 'Phase', value: c.schedulePhase),
              if (c.scheduleFrames > 0)
                _DiagRow(
                  label: 'Exposure progress',
                  value: '${c.scheduleFrame} / ${c.scheduleFrames}',
                ),
              if (c.scheduleTotal > 0)
                _DiagRow(
                  label: 'Plan progress',
                  value: '${c.scheduleCompleted} / ${c.scheduleTotal}',
                ),
              if (c.scheduleError != null && c.scheduleError!.isNotEmpty)
                _DiagRow(label: 'Last error', value: c.scheduleError!),
              _DiagRow(
                label: 'Plan items tonight',
                value: '${timeline.length}',
              ),
            ],
          ),
          _DiagCard(
            title: 'Cloud link',
            children: [
              _DiagRow(label: 'Registered', value: _tri(c.cloudRegistered)),
              _DiagRow(label: 'Cloud heartbeat', value: _tri(c.lastHeartbeatOk)),
              _DiagRow(label: 'Auto-run plans', value: _tri(c.autoRunPlans)),
              _DiagRow(label: 'Photometry', value: _tri(c.photometryEnabled)),
              if (c.lastPlanId != null && c.lastPlanId!.isNotEmpty)
                _DiagRow(label: 'Last plan ID', value: c.lastPlanId!),
              _DiagRow(label: 'Plan items cached', value: '${c.planItems}'),
            ],
          ),
          if (timeline.isNotEmpty)
            _DiagCard(
              title: 'Tonight\'s queue',
              children: [
                for (final item in timeline.take(8))
                  _DiagRow(
                    label: item.startTime.isEmpty ? '—' : item.startTime,
                    value: item.target.isEmpty ? 'Scheduled' : item.target,
                  ),
              ],
            ),
          _RawConditionsBlock(conditions: c),
        ],
      ),
    );
  }

  String _statusLabel(String status) {
    switch (status) {
      case 'active':
        return 'Active';
      case 'sleeping':
        return 'Sleeping';
      case 'vacation':
        return 'Vacation';
      case 'disabled':
        return 'Disabled';
      default:
        return status.isEmpty ? 'Unknown' : status;
    }
  }

  String _tri(bool? v) {
    if (v == null) return '—';
    return v ? 'Yes' : 'No';
  }
}

class _RawConditionsBlock extends StatefulWidget {
  const _RawConditionsBlock({required this.conditions});
  final NodeConditions conditions;

  @override
  State<_RawConditionsBlock> createState() => _RawConditionsBlockState();
}

class _RawConditionsBlockState extends State<_RawConditionsBlock> {
  bool _open = false;

  @override
  Widget build(BuildContext context) {
    final encoder = const JsonEncoder.withIndent('  ');
    final map = <String, dynamic>{
      if (widget.conditions.safe != null) 'safe': widget.conditions.safe,
      if (widget.conditions.reason.isNotEmpty)
        'reason': widget.conditions.reason,
      if (widget.conditions.sunElevation != null)
        'sun_elevation': widget.conditions.sunElevation,
      if (widget.conditions.dawnThreshold != null)
        'dawn_threshold': widget.conditions.dawnThreshold,
      'schedule_running': widget.conditions.scheduleRunning,
      if (widget.conditions.scheduleTarget.isNotEmpty)
        'schedule_target': widget.conditions.scheduleTarget,
      if (widget.conditions.schedulePhase.isNotEmpty)
        'schedule_phase': widget.conditions.schedulePhase,
      if (widget.conditions.autoRunPlans != null)
        'auto_run_plans': widget.conditions.autoRunPlans,
      if (widget.conditions.telescopeConnected != null)
        'telescope_connected': widget.conditions.telescopeConnected,
    };
    final raw = encoder.convert(map);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        InkWell(
          onTap: () => setState(() => _open = !_open),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            child: Row(
              children: [
                Icon(
                  _open ? Icons.expand_less : Icons.expand_more,
                  size: 18,
                  color: BSTheme.ink3,
                ),
                const SizedBox(width: 8),
                const Text(
                  'Raw heartbeat payload',
                  style: TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    color: BSTheme.ink2,
                  ),
                ),
              ],
            ),
          ),
        ),
        if (_open)
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 0, 14, 14),
            child: SelectableText(
              raw,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 11,
                color: BSTheme.ink3,
                height: 1.45,
              ),
            ),
          ),
      ],
    );
  }
}

class _SectionTitle extends StatelessWidget {
  const _SectionTitle({
    required this.label,
    required this.title,
    this.subtitle,
  });

  final String label;
  final String title;
  final String? subtitle;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: const TextStyle(
            fontFamily: 'Geist',
            fontSize: 10,
            fontWeight: FontWeight.w900,
            letterSpacing: 1.2,
            color: BSTheme.ink3,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          title,
          style: const TextStyle(
            fontFamily: 'Geist',
            fontSize: 22,
            fontWeight: FontWeight.w800,
            color: BSTheme.ink,
          ),
        ),
        if (subtitle != null) ...[
          const SizedBox(height: 6),
          Text(
            subtitle!,
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 13,
              color: BSTheme.ink3,
              height: 1.4,
            ),
          ),
        ],
      ],
    );
  }
}

class _DiagCard extends StatelessWidget {
  const _DiagCard({required this.title, required this.children});

  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            title.toUpperCase(),
            style: const TextStyle(
              fontFamily: 'Geist',
              fontSize: 10,
              fontWeight: FontWeight.w800,
              letterSpacing: 1.0,
              color: BSTheme.ink3,
            ),
          ),
          const SizedBox(height: 8),
          ...children,
        ],
      ),
    );
  }
}

class _DiagRow extends StatelessWidget {
  const _DiagRow({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 132,
            child: Text(
              label,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                color: BSTheme.ink3,
              ),
            ),
          ),
          Expanded(
            child: Text(
              value,
              style: const TextStyle(
                fontFamily: 'Geist',
                fontSize: 12,
                fontWeight: FontWeight.w700,
                color: BSTheme.ink2,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Pill extends StatelessWidget {
  const _Pill({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: color.withValues(alpha: 0.3)),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontFamily: 'Geist',
          fontSize: 9,
          fontWeight: FontWeight.w800,
          letterSpacing: 0.8,
          color: color,
        ),
      ),
    );
  }
}