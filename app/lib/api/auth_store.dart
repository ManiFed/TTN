import 'package:shared_preferences/shared_preferences.dart';

/// Persists the bearer token + user id issued by the cloud on login/register.
class AuthStore {
  static const _kToken = 'bs_auth_token';
  static const _kUserId = 'bs_user_id';

  String? token;
  String? userId;

  bool get isLoggedIn => token != null && token!.isNotEmpty;

  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    token = prefs.getString(_kToken);
    userId = prefs.getString(_kUserId);
  }

  Future<void> save(String token, String userId) async {
    this.token = token;
    this.userId = userId;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_kToken, token);
    await prefs.setString(_kUserId, userId);
  }

  Future<void> clear() async {
    token = null;
    userId = null;
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_kToken);
    await prefs.remove(_kUserId);
  }
}
