library;

class AppConfig {
  /// All cloud routes are versioned under /api/v1.
  static const String apiPrefix = '/api/v1';

  /// Production cloud URL used by native (iOS/Android) builds.
  static const String _productionBase = 'https://api.thetelescope.net';

  /// For web builds, derives the API base from the page origin so dev and prod
  /// work without a rebuild. For native builds, Uri.base is a file:// URI so
  /// origin is empty — fall back to the hardcoded production URL.
  /// Override with --dart-define=API_BASE=http://localhost:8800 for local dev.
  static String get apiBase {
    const override = String.fromEnvironment('API_BASE');
    if (override.isNotEmpty) return override;
    final origin = Uri.base.origin;
    if (origin.isEmpty || origin == 'null') return _productionBase;
    return origin;
  }

  static Uri uri(String path, [Map<String, dynamic>? query]) {
    final q = query?.map((k, v) => MapEntry(k, '$v'));
    return Uri.parse('$apiBase$apiPrefix$path').replace(queryParameters: q);
  }
}
