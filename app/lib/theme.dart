import 'package:flutter/material.dart';

/// Disability-first theme for Boundless Skies.
///
/// Design goals (see VISION.md): large default type, high-contrast colours,
/// generous touch targets (≥48dp), and never relying on colour alone to carry
/// meaning. The night-sky palette doubles as a comfortable dark default.
class BSTheme {
  // Brand palette — deep sky blues with a warm "starlight" accent.
  static const Color _night = Color(0xFF0B1026);
  static const Color _surface = Color(0xFF161C3A);
  static const Color _starlight = Color(0xFFFFC857);
  static const Color _skyBlue = Color(0xFF7DA9FF);

  static const Color success = Color(0xFF5BD6A6);
  static const Color warning = Color(0xFFFFB454);
  static const Color danger = Color(0xFFFF6B6B);

  static ThemeData dark() {
    final scheme = const ColorScheme.dark(
      primary: _skyBlue,
      onPrimary: _night,
      secondary: _starlight,
      onSecondary: _night,
      surface: _surface,
      onSurface: Colors.white,
      error: danger,
    );

    final base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      colorScheme: scheme,
      scaffoldBackgroundColor: _night,
      visualDensity: VisualDensity.comfortable,
    );

    return base.copyWith(
      // Slightly enlarged type scale for low-vision readability.
      textTheme: base.textTheme.apply(fontSizeFactor: 1.1),
      appBarTheme: const AppBarTheme(
        backgroundColor: _night,
        elevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(
          fontSize: 24,
          fontWeight: FontWeight.w700,
          color: Colors.white,
        ),
      ),
      cardTheme: CardThemeData(
        color: _surface,
        elevation: 0,
        margin: const EdgeInsets.symmetric(vertical: 6),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: _skyBlue,
          foregroundColor: _night,
          // 48dp minimum target height for motor-accessibility.
          minimumSize: const Size.fromHeight(56),
          textStyle: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: _surface,
        contentPadding: const EdgeInsets.symmetric(horizontal: 18, vertical: 18),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: BorderSide.none,
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: _skyBlue, width: 2),
        ),
        labelStyle: const TextStyle(fontSize: 17),
      ),
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: _surface,
        indicatorColor: _skyBlue.withValues(alpha: 0.25),
        labelTextStyle: WidgetStateProperty.all(
          const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
        ),
      ),
      snackBarTheme: const SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: _surface,
        contentTextStyle: TextStyle(fontSize: 16, color: Colors.white),
      ),
    );
  }
}
