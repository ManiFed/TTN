import 'dart:convert';

import 'package:http/http.dart' as http;

/// Read-only client for the node agent running on this computer.
///
/// The desktop application owns the foreground experience; telescope control,
/// image processing, and safety work remain in the background Python service.
class NodeAgentClient {
  NodeAgentClient({http.Client? client}) : _http = client ?? http.Client();

  static final Uri _statusUri = Uri.parse('http://127.0.0.1:5173/api/status');
  final http.Client _http;

  Future<NodeAgentStatus> status() async {
    final response = await _http
        .get(_statusUri)
        .timeout(const Duration(seconds: 3));
    if (response.statusCode != 200) {
      throw NodeAgentException('The local node service returned ${response.statusCode}.');
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map<String, dynamic>) {
      throw const NodeAgentException('The local node service sent an invalid response.');
    }
    return NodeAgentStatus.fromJson(decoded);
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
  });

  final bool connected;
  final bool telescopeConnected;
  final bool cameraConnected;
  final bool safe;
  final String safetyReason;
  final bool photometryRunning;
  final int queuedFrames;
  final String? commissioningStatus;

  factory NodeAgentStatus.fromJson(Map<String, dynamic> json) {
    final telescope = (json['telescope'] as Map?)?.cast<String, dynamic>() ?? const {};
    final camera = (json['camera'] as Map?)?.cast<String, dynamic>() ?? const {};
    final safety = (json['safety'] as Map?)?.cast<String, dynamic>() ?? const {};
    final photometry = (json['photometry'] as Map?)?.cast<String, dynamic>() ?? const {};
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
    );
  }
}
