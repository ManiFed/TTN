import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

/// Client for the node agent running on this computer.
class NodeAgentClient {
  NodeAgentClient({http.Client? client}) : _http = client ?? http.Client();

  static final Uri _base = Uri.parse('http://127.0.0.1:5173');
  final http.Client _http;

  /// Run one request, reporting every failure as a [NodeAgentException].
  ///
  /// A refused connection or a timeout arrives as SocketException /
  /// ClientException / TimeoutException — none of which are
  /// NodeAgentException. Callers that guard with `on NodeAgentException`
  /// were therefore letting exactly the common failures (agent still
  /// starting, or busy enough that a slow endpoint times out) escape as
  /// unhandled errors, which turned a recoverable "agent not reachable
  /// yet" into a total failure of whatever the user was doing.
  Future<http.Response> _guard(
    Future<http.Response> Function() send,
    String what,
  ) async {
    try {
      return await send();
    } on NodeAgentException {
      rethrow;
    } on TimeoutException {
      throw NodeAgentException(
        'The node software on this computer did not respond in time '
        '($what). It may still be starting up — try again in a moment.',
      );
    } catch (_) {
      throw NodeAgentException(
        'Could not reach the node software on this computer ($what). '
        'Make sure it is running, then try again.',
      );
    }
  }

  Future<NodeAgentStatus> status() async {
    // /api/status does real work on the agent (camera state, commissioning
    // checks, AAVSO, etc.) and can be slow right after other agent activity,
    // so give it more room than a simple health check would need.
    final response = await _guard(
      () => _http
          .get(_base.replace(path: '/api/status'))
          .timeout(const Duration(seconds: 8)),
      'reading status',
    );
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
    final response = await _guard(
      () => _http
          .post(
            _base.replace(path: '/api/cloud/credentials'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({'node_id': nodeId, 'api_key': apiKey}),
          )
          .timeout(const Duration(seconds: 15)),
      'installing credentials',
    );
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

  /// Broadcast an ALPACA discovery request on this computer's local network
  /// and return every server that responded.
  Future<List<Map<String, dynamic>>> discoverAlpacaServers() async {
    final response = await _guard(
      () => _http
          .post(_base.replace(path: '/api/discover'))
          .timeout(const Duration(seconds: 10)),
      'searching for the telescope',
    );
    if (response.statusCode != 200) {
      throw NodeAgentException(
        'ALPACA discovery failed (${response.statusCode}).',
      );
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map<String, dynamic>) {
      throw const NodeAgentException(
        'The local node service sent an invalid discovery response.',
      );
    }
    final servers = decoded['servers'];
    if (servers is! List) return const [];
    return servers.whereType<Map>().map((s) => s.cast<String, dynamic>()).toList();
  }

  /// Tell the node agent to connect to the ALPACA server at [host]:[port].
  Future<void> connectAlpaca({
    required String host,
    required int port,
    bool setAsDefault = false,
  }) async {
    final response = await _guard(
      () => _http
          .post(
            _base.replace(path: '/api/connect'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'host': host,
              'port': port,
              'set_as_default': setAsDefault,
            }),
          )
          .timeout(const Duration(seconds: 15)),
      'connecting the telescope',
    );
    if (response.statusCode != 200) {
      String detail = 'Could not connect to the telescope server (${response.statusCode}).';
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
