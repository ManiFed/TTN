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
    } catch (_) {
      // Offline but we have a token — let them in optimistically.
      status = AuthStatus.signedIn;
    }
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

  Future<void> signOut() async {
    await _auth.clear();
    member = null;
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
