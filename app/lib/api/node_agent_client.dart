import 'dart:convert';

import 'package:http/http.dart' as http;

/// Client for the node agent running on this computer.
///
/// The desktop application owns the foreground experience; telescope control,
/// image processing, and safety work remain in the background Python service.
class NodeAgentClient {
  NodeAgentClient({http.Client? client}) : _http = client ?? http.Client();

  static final Uri _base = Uri.parse('http://127.0.0.1:5173');
  final http.Client _http;

  Future<NodeAgentStatus> status() async {
    final response = await _http
        .get(_base.replace(path: '/api/status'))
        .timeout(const Duration(seconds: 3));
    if (response.statusCode != 200) {
      throw NodeAgentException(
        'The local node service returned ${response.statusCode}.',
      );
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map<String, dynamic>) {
      throw const NodeAgentException(
        'The local node service sent an invalid response.',
      );
    }

    var cloudRegistered = false;
    try {
      final cloudResp = await _http
          .get(_base.replace(path: '/api/cloud'))
          .timeout(const Duration(seconds: 3));
      if (cloudResp.statusCode == 200) {
        final cloud = jsonDecode(cloudResp.body);
        if (cloud is Map && cloud['registered'] == true) {
          cloudRegistered = true;
        }
      }
    } catch (_) {
      // Cloud block is optional for the status card.
    }

    return NodeAgentStatus.fromJson(decoded, cloudRegistered: cloudRegistered);
  }

  /// Save an activation code on the local agent and register with the cloud.
  Future<void> activate(String code) async {
    final trimmed = code.trim().toUpperCase();
    if (trimmed.isEmpty) {
      throw const NodeAgentException('Enter an activation code.');
    }

    final cfgResp = await _http
        .get(_base.replace(path: '/api/config/parsed'))
        .timeout(const Duration(seconds: 5));
    if (cfgResp.statusCode != 200) {
      throw NodeAgentException(
        'Could not read local node config (${cfgResp.statusCode}).',
      );
    }
    final cfg = jsonDecode(cfgResp.body);
    if (cfg is! Map<String, dynamic>) {
      throw const NodeAgentException('Local node config was invalid.');
    }

    final cloud = (cfg['cloud'] is Map)
        ? Map<String, dynamic>.from(cfg['cloud'] as Map)
        : <String, dynamic>{};
    cloud['enabled'] = true;
    cloud['activation_code'] = trimmed;
    if ((cloud['url'] as String?)?.isEmpty ?? true) {
      cloud['url'] = 'https://api.thetelescope.net';
    }
    cfg['cloud'] = cloud;

    final saveResp = await _http
        .post(
          _base.replace(path: '/api/config/parsed'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(cfg),
        )
        .timeout(const Duration(seconds: 8));
    if (saveResp.statusCode != 200) {
      String detail = 'Could not save activation code (${saveResp.statusCode}).';
      try {
        final body = jsonDecode(saveResp.body);
        if (body is Map && body['error'] != null) {
          detail = body['error'].toString();
        }
      } catch (_) {}
      throw NodeAgentException(detail);
    }

    final connectResp = await _http
        .post(_base.replace(path: '/api/cloud/connect'))
        .timeout(const Duration(seconds: 20));
    final connectBody = () {
      try {
        return jsonDecode(connectResp.body);
      } catch (_) {
        return null;
      }
    }();
    if (connectResp.statusCode != 200 ||
        connectBody is! Map ||
        connectBody['ok'] != true) {
      final err = (connectBody is Map ? connectBody['error'] : null)?.toString()
          ?? 'Registration failed (${connectResp.statusCode}).';
      throw NodeAgentException(err);
    }
  }
}

class NodeAgentException implements Exception {
  const NodeAgentException(this.message);
  final String message;

  @override
  String toString() => message;
}

class NodeAgentStatus {
  const NodeAgentStatus({
    required this.connected,
    required this.telescopeConnected,
    required this.cameraConnected,
    required this.safe,
    required this.safetyReason,
    required this.photometryRunning,
    required this.queuedFrames,
    required this.commissioningStatus,
    required this.cloudRegistered,
  });

  final bool connected;
  final bool telescopeConnected;
  final bool cameraConnected;
  final bool safe;
  final String safetyReason;
  final bool photometryRunning;
  final int queuedFrames;
  final String? commissioningStatus;
  final bool cloudRegistered;

  factory NodeAgentStatus.fromJson(
    Map<String, dynamic> json, {
    bool cloudRegistered = false,
  }) {
    final telescope =
        (json['telescope'] as Map?)?.cast<String, dynamic>() ?? const {};
    final camera =
        (json['camera'] as Map?)?.cast<String, dynamic>() ?? const {};
    final safety =
        (json['safety'] as Map?)?.cast<String, dynamic>() ?? const {};
    final photometry =
        (json['photometry'] as Map?)?.cast<String, dynamic>() ?? const {};
    final commissioning = (json['commissioning'] as Map?)?.cast<String, dynamic>();
    return NodeAgentStatus(
      connected: json['connected'] == true,
      telescopeConnected: telescope['connected'] == true,
      cameraConnected: camera['connected'] == true,
      safe: safety['safe'] != false,
      safetyReason: safety['reason'] as String? ?? '',
      photometryRunning: photometry['running'] == true,
      queuedFrames: (photometry['queued'] as num?)?.toInt() ?? 0,
      commissioningStatus: commissioning?['status'] as String?,
      cloudRegistered: cloudRegistered,
    );
  }
}
