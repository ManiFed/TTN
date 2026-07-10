import 'dart:convert';

import 'package:http/http.dart' as http;

/// Client for the node agent running on this computer.
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
    String? pairToken;
    String? nodeId;
    try {
      final cloudResp = await _http
          .get(_base.replace(path: '/api/cloud'))
          .timeout(const Duration(seconds: 3));
      if (cloudResp.statusCode == 200) {
        final cloud = jsonDecode(cloudResp.body);
        if (cloud is Map) {
          cloudRegistered = cloud['registered'] == true;
          pairToken = cloud['pair_token'] as String?;
          nodeId = cloud['node_id'] as String?;
        }
      }
    } catch (_) {}

    return NodeAgentStatus.fromJson(
      decoded,
      cloudRegistered: cloudRegistered,
      pairToken: pairToken,
      nodeId: nodeId,
    );
  }

  /// Install cloud credentials produced by POST /me/nodes/attach.
  Future<void> installCredentials({
    required String nodeId,
    required String apiKey,
  }) async {
    final response = await _http
        .post(
          _base.replace(path: '/api/cloud/credentials'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'node_id': nodeId, 'api_key': apiKey}),
        )
        .timeout(const Duration(seconds: 8));
    if (response.statusCode != 200) {
      String detail = 'Could not install credentials (${response.statusCode}).';
      try {
        final body = jsonDecode(response.body);
        if (body is Map && body['error'] != null) {
          detail = body['error'].toString();
        }
      } catch (_) {}
      throw NodeAgentException(detail);
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
    this.pairToken,
    this.nodeId,
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
  final String? pairToken;
  final String? nodeId;

  factory NodeAgentStatus.fromJson(
    Map<String, dynamic> json, {
    bool cloudRegistered = false,
    String? pairToken,
    String? nodeId,
  }) {
    final telescope =
        (json['telescope'] as Map?)?.cast<String, dynamic>() ?? const {};
    final camera =
        (json['camera'] as Map?)?.cast<String, dynamic>() ?? const {};
    final safety =
        (json['safety'] as Map?)?.cast<String, dynamic>() ?? const {};
    final photometry =
        (json['photometry'] as Map?)?.cast<String, dynamic>() ?? const {};
    final commissioning =
        (json['commissioning'] as Map?)?.cast<String, dynamic>();
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
      pairToken: pairToken,
      nodeId: nodeId,
    );
  }
}
