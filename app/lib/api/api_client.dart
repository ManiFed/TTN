import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config.dart';
import '../models/models.dart';
import 'auth_store.dart';

/// Thrown when the cloud returns a non-2xx response. [statusCode] 401 means the
/// token is invalid/expired and the caller should sign the member out.
class ApiException implements Exception {
  final int statusCode;
  final String message;
  const ApiException(this.statusCode, this.message);

  bool get isUnauthorized => statusCode == 401;

  @override
  String toString() => message;
}

/// Typed wrapper over the cloud member API (cloud/server.py, /api/v1/*).
class ApiClient {
  ApiClient(this._auth, {http.Client? client}) : _http = client ?? http.Client();

  final AuthStore _auth;
  final http.Client _http;

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        if (_auth.token != null) 'Authorization': 'Bearer ${_auth.token}',
      };

  Future<Map<String, dynamic>> _get(String path, [Map<String, dynamic>? query]) async {
    final res = await _http.get(AppConfig.uri(path, query), headers: _headers);
    return _decode(res);
  }

  Future<Map<String, dynamic>> _post(String path, Map<String, dynamic> body) async {
    final res = await _http.post(
      AppConfig.uri(path),
      headers: _headers,
      body: jsonEncode(body),
    );
    return _decode(res);
  }

  Future<Map<String, dynamic>> _put(String path, Map<String, dynamic> body) async {
    final res = await _http.put(
      AppConfig.uri(path),
      headers: _headers,
      body: jsonEncode(body),
    );
    return _decode(res);
  }

  Future<Map<String, dynamic>> _delete(String path) async {
    final res = await _http.delete(AppConfig.uri(path), headers: _headers);
    return _decode(res);
  }

  Map<String, dynamic> _decode(http.Response res) {
    Map<String, dynamic> json;
    try {
      json = res.body.isEmpty ? {} : jsonDecode(res.body) as Map<String, dynamic>;
    } catch (_) {
      throw ApiException(res.statusCode, 'Unexpected response from server.');
    }
    if (res.statusCode < 200 || res.statusCode >= 300) {
      throw ApiException(
        res.statusCode,
        (json['error'] as String?) ?? 'Request failed (${res.statusCode}).',
      );
    }
    return json;
  }

  // ── Auth ─────────────────────────────────────────────────────────────────

  /// Registers a new member and persists the returned token.
  Future<void> register(String email, String password, String displayName) async {
    final json = await _post('/auth/register', {
      'email': email,
      'password': password,
      'display_name': displayName,
    });
    await _auth.save(json['token'] as String, json['user_id'] as String);
  }

  /// Logs in and persists the returned token.
  Future<void> login(String email, String password) async {
    final json = await _post('/auth/login', {'email': email, 'password': password});
    await _auth.save(json['token'] as String, json['user_id'] as String);
  }

  /// Revokes this device's session only -- other signed-in devices are
  /// unaffected. Best-effort: local sign-out proceeds even if this fails
  /// (e.g. already offline).
  Future<void> logout() async {
    try {
      await _post('/auth/logout', {});
    } catch (_) {}
  }

  // ── Member data ──────────────────────────────────────────────────────────

  Future<Member> me() async => Member.fromJson(await _get('/me'));

  Future<MemberStats> stats() async => MemberStats.fromJson(await _get('/me/stats'));

  Future<List<Node>> nodes() async {
    final json = await _get('/me/nodes');
    return ((json['nodes'] as List?) ?? [])
        .map((e) => Node.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<void> claimNode(String nodeId, String apiKey) =>
      _post('/me/nodes/$nodeId', {'api_key': apiKey});

  Future<List<Observation>> observations({int days = 90, int limit = 200}) async {
    final json = await _get('/me/observations', {'days': days, 'limit': limit});
    return ((json['observations'] as List?) ?? [])
        .map((e) => Observation.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<List<TimelineItem>> timeline() async {
    final json = await _get('/me/timeline');
    return ((json['items'] as List?) ?? [])
        .map((e) => TimelineItem.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Returns notifications and the unread count.
  Future<(List<AppNotification>, int)> notifications({int limit = 50}) async {
    final json = await _get('/me/notifications', {'limit': limit});
    final list = ((json['notifications'] as List?) ?? [])
        .map((e) => AppNotification.fromJson(e as Map<String, dynamic>))
        .toList();
    return (list, (json['unread'] as int?) ?? 0);
  }

  Future<void> markNotificationRead(int id) => _post('/me/notifications/$id/read', {});

  /// Public telescope spec catalog (GET /telescopes) for the connect-flow picker.
  Future<List<TelescopeSpec>> telescopes() async {
    final json = await _get('/telescopes');
    return ((json['telescopes'] as List?) ?? [])
        .map((e) => TelescopeSpec.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Newest published node/app version + where to get it, for the
  /// update-available banner. Public endpoint — no auth required, and
  /// failures should never surface (returns null instead of throwing).
  Future<(String, String)?> latestVersion() async {
    try {
      final json = await _get('/versions');
      final version = json['latest'] as String?;
      if (version == null) return null;
      return (version, json['download_page'] as String? ?? '');
    } catch (_) {
      return null;
    }
  }

  /// Register/link a telescope to this member account (no activation codes).
  /// Returns {node_id, api_key, linked}.
  Future<Map<String, dynamic>> attachNode({
    String? locationName,
    double? lat,
    double? lon,
    String? telescopeModel,
    String? telescopeDisplayName,
    Map<String, dynamic>? telescopeSpecs,
    bool portable = false,
    String? existingNodeId,
    String? existingApiKey,
  }) async {
    final body = <String, dynamic>{};
    if (lat != null && lon != null) {
      body['latitude'] = lat;
      body['longitude'] = lon;
    }
    if (locationName != null && locationName.isNotEmpty) {
      body['location_name'] = locationName;
    }
    if (telescopeModel != null && telescopeModel.isNotEmpty) {
      body['telescope_model'] = telescopeModel;
    }
    if (telescopeDisplayName != null && telescopeDisplayName.isNotEmpty) {
      body['telescope_display_name'] = telescopeDisplayName;
    }
    if (telescopeSpecs != null && telescopeSpecs.isNotEmpty) {
      body['telescope_specs'] = telescopeSpecs;
    }
    if (portable) body['portable'] = true;
    if (existingNodeId != null && existingNodeId.isNotEmpty) {
      body['node_id'] = existingNodeId;
    }
    if (existingApiKey != null && existingApiKey.isNotEmpty) {
      body['api_key'] = existingApiKey;
    }
    return _post('/me/nodes/attach', body);
  }

  /// Start tonight's observing session for a portable node.
  /// Returns {mpsas, bortle} for the session location.
  Future<Map<String, dynamic>> startNodeSession(
    String nodeId, {
    required double lat,
    required double lon,
    required String city,
    String siteName = '',
  }) =>
      _post('/me/nodes/$nodeId/session', {
        'lat': lat,
        'lon': lon,
        'city': city,
        'site_name': siteName,
      });

  /// End a portable node's session early (returns it to sleeping).
  Future<void> endNodeSession(String nodeId) => _delete('/me/nodes/$nodeId/session');

  /// Put a node on vacation until [untilDate] ('YYYY-MM-DD').
  Future<void> setNodeVacation(String nodeId, String untilDate) =>
      _put('/me/nodes/$nodeId/vacation', {'until_date': untilDate});

  /// Cancel a node's active vacation.
  Future<void> cancelNodeVacation(String nodeId) => _delete('/me/nodes/$nodeId/vacation');

  /// Disconnect a node from this account.
  Future<void> disconnectNode(String nodeId) => _delete('/me/nodes/$nodeId');

  /// Set the member's custom display name for a claimed node.
  Future<void> updateNodeDisplayName(String nodeId, String displayName) =>
      _put('/me/nodes/$nodeId', {'display_name': displayName});

  /// Fetch sky quality (mpsas + bortle) for a lat/lon without starting a session.
  Future<Map<String, dynamic>> skyQuality(double lat, double lon) =>
      _get('/sky-quality', {'lat': lat, 'lon': lon});

  /// Live fleet: live fleet phases + the network's own mid-night reflow
  /// and reflex-confirmation activity. Public read, no auth required.
  Future<LiveFleetFeed> liveFleet() async =>
      LiveFleetFeed.fromJson(await _get('/network/live-fleet'));

  /// Open Aperture: discovery candidates this member's nodes or contributed
  /// frames touched — "your telescope may have found something new".
  Future<List<Discovery>> discoveries() async {
    final json = await _get('/me/discoveries');
    return (json['discoveries'] as List? ?? [])
        .map((e) => Discovery.fromJson(Map<String, dynamic>.from(e as Map)))
        .toList();
  }

  /// Push credentials to a local agent that is polling a pairing token.
  Future<void> pushPairCredentials({
    required String pairingToken,
    required String nodeId,
    required String apiKey,
  }) =>
      _post('/nodes/pair', {
        'pairing_token': pairingToken.trim().toUpperCase(),
        'node_id': nodeId,
        'api_key': apiKey,
      });

  Future<void> setNotificationPrefs({bool? email, bool? push, String? pushToken}) =>
      _put('/me/notifications/prefs', {
        if (email != null) 'notification_email': email,
        if (push != null) 'notification_push': push,
        if (pushToken != null) 'push_token': pushToken,
      });

  Future<void> deleteAccount() async {
    final res = await _http.delete(
      AppConfig.uri('/me'),
      headers: _headers,
      body: jsonEncode({'confirm': true}),
    );
    _decode(res);
  }

  /// Night summaries across all member nodes, newest first (GET /me/nights).
  Future<List<NightSummary>> nights({int limit = 30}) async {
    final json = await _get('/me/nights', {'limit': limit});
    return ((json['nights'] as List?) ?? [])
        .map((e) => NightSummary.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Active targets sorted by network priority (GET /targets — public).
  Future<List<Target>> targets() async {
    final json = await _get('/targets');
    return ((json['targets'] as List?) ?? [])
        .map((e) => Target.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Photometric light curve for one target (GET /lightcurves/<name>?days=<n>).
  Future<List<LightCurvePoint>> lightCurve(String targetName,
      {int days = 90}) async {
    final path = '/lightcurves/${Uri.encodeComponent(targetName)}';
    final json = await _get(path, {'days': days});
    return ((json['points'] as List?) ?? [])
        .map((e) => LightCurvePoint.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Public catalogue details for one object (GET /objects/<name>).
  Future<ObjectDetails> objectDetails(String objectName) async {
    final path = '/objects/${Uri.encodeComponent(objectName)}';
    return ObjectDetails.fromJson(await _get(path));
  }

  /// Submit a science program suggestion (POST /me/science-program-suggestions).
  Future<void> suggestScienceProgram({
    required String title,
    required String description,
    String targetExamples = '',
    String notes = '',
  }) async {
    await _post('/me/science-program-suggestions', {
      'title': title,
      'description': description,
      if (targetExamples.isNotEmpty) 'target_examples': targetExamples,
      if (notes.isNotEmpty) 'notes': notes,
    });
  }

  /// Help tab session: contact info, quota, chat history (GET /me/help).
  Future<HelpSession> helpSession() async =>
      HelpSession.fromJson(await _get('/me/help'));

  /// Send one help message (POST /me/help/chat). Limited to 5 user messages/week.
  Future<HelpChatResponse> helpChat(String message, {String? nodeId}) async {
    final body = <String, dynamic>{'message': message};
    if (nodeId != null && nodeId.isNotEmpty) body['node_id'] = nodeId;
    return HelpChatResponse.fromJson(await _post('/me/help/chat', body));
  }

  void close() => _http.close();
}
