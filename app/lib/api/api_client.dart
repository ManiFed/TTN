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

  /// Returns notifications and the unread count.
  Future<(List<AppNotification>, int)> notifications({int limit = 50}) async {
    final json = await _get('/me/notifications', {'limit': limit});
    final list = ((json['notifications'] as List?) ?? [])
        .map((e) => AppNotification.fromJson(e as Map<String, dynamic>))
        .toList();
    return (list, (json['unread'] as int?) ?? 0);
  }

  Future<void> markNotificationRead(int id) => _post('/me/notifications/$id/read', {});

  Future<String> generateActivationCode() async {
    final json = await _post('/me/activation-code', {});
    return json['code'] as String;
  }

  Future<void> setNotificationPrefs({bool? email, bool? push, String? pushToken}) =>
      _put('/me/notifications/prefs', {
        if (email != null) 'notification_email': email,
        if (push != null) 'notification_push': push,
        if (pushToken != null) 'push_token': pushToken,
      });

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

  void close() => _http.close();
}
