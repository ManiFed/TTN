/// App-wide configuration.
///
/// The cloud layer runs Flask + SQLite on port 8800 (see cloud/main.py).
/// Override the base URL at build time with:
///   flutter run --dart-define=BS_API_BASE=https://api.telescopenet.org
library;

class AppConfig {
  /// Base URL of the The Telescope Net cloud API (no trailing slash).
  static const String apiBase = String.fromEnvironment(
    'BS_API_BASE',
    defaultValue: 'http://localhost:8800',
  );

  /// All cloud routes are versioned under /api/v1.
  static const String apiPrefix = '/api/v1';

  static Uri uri(String path, [Map<String, dynamic>? query]) {
    final q = query?.map((k, v) => MapEntry(k, '$v'));
    return Uri.parse('$apiBase$apiPrefix$path').replace(queryParameters: q);
  }
}
