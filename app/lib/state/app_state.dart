import 'package:flutter/foundation.dart';

import '../api/api_client.dart';
import '../api/auth_store.dart';
import '../models/models.dart';

enum AuthStatus { unknown, signedOut, signedIn }

/// Single source of truth for session + member data. Screens listen via Provider.
class AppState extends ChangeNotifier {
  AppState(this._auth) : _api = ApiClient(_auth);

  final AuthStore _auth;
  final ApiClient _api;

  ApiClient get api => _api;

  AuthStatus status = AuthStatus.unknown;
  Member? member;
  String? lastError;

  /// Whether the member has at least one claimed node. Null = not yet checked.
  bool? _hasNode;
  bool get hasNode => _hasNode == true;
  bool get nodesLoaded => _hasNode != null;

  /// Set by PushService when a notification is tapped while app is cold/backgrounded.
  /// HomeScreen reads and clears this to jump to the right tab.
  int? pendingTab;

  /// Loads any persisted token and probes whether it is still valid.
  Future<void> bootstrap() async {
    await _auth.load();
    if (!_auth.isLoggedIn) {
      status = AuthStatus.signedOut;
      notifyListeners();
      return;
    }
    try {
      member = await _api.me();
      status = AuthStatus.signedIn;
    } on ApiException catch (e) {
      if (e.isUnauthorized) {
        await _auth.clear();
      }
      status = AuthStatus.signedOut;
      notifyListeners();
      return;
    } catch (_) {
      // Offline but we have a token — let them in optimistically.
      status = AuthStatus.signedIn;
      notifyListeners();
      return;
    }
    await _fetchNodes();
    notifyListeners();
  }

  Future<bool> login(String email, String password) =>
      _authFlow(() => _api.login(email, password));

  Future<bool> register(String email, String password, String name) =>
      _authFlow(() => _api.register(email, password, name));

  Future<bool> _authFlow(Future<void> Function() action) async {
    lastError = null;
    notifyListeners();
    try {
      await action();
      member = await _api.me();
      status = AuthStatus.signedIn;
      await _fetchNodes();
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      lastError = e.message;
      notifyListeners();
      return false;
    } catch (_) {
      lastError = 'Could not reach the network. Check your connection and try again.';
      notifyListeners();
      return false;
    }
  }

  /// Re-check node count — call after a node is claimed in the setup flow.
  Future<void> refreshNodes() async {
    await _fetchNodes();
    notifyListeners();
  }

  Future<void> _fetchNodes() async {
    try {
      final nodes = await _api.nodes();
      _hasNode = nodes.isNotEmpty;
    } catch (_) {
      // Network failure — don't gate the UI; assume connected and let tabs show empty.
      _hasNode = true;
    }
  }

  Future<void> signOut() async {
    await _auth.clear();
    member = null;
    _hasNode = null;
    status = AuthStatus.signedOut;
    notifyListeners();
  }

  /// Centralised handler so a 401 anywhere drops the member to the login screen.
  void handleAuthError(Object error) {
    if (error is ApiException && error.isUnauthorized) {
      signOut();
    }
  }
}
