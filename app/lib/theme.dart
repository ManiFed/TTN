import 'package:flutter/material.dart';

class BSTheme {
  // Operations-console tokens: restrained, legible, and status-led.
  static const Color night = Color(0xFF07090C);
  static const Color surface = Color(0xFF0D1014);
  static const Color surface2 = Color(0xFF12161C);
  static const Color ink = Color(0xFFF1F0E8);
  static const Color ink2 = Color(0xA6F1F0E8); // 65% opacity
  static const Color ink3 = Color(0x66F1F0E8); // 40% opacity
  static const Color accent = Color(0xFF5BD6A6);
  static const Color sky = Color(0xFF8FD9FF);
  static const Color warm = Color(0xFFFFC07A);
  static const Color success = Color(0xFF5BD6A6);
  static const Color warning = warm;
  static const Color danger = Color(0xFFFF6B6B);
  static const Color glassBorder = Color(0x2EF1F0E8);
  static const Color glassBg = Color(0x0AF1F0E8);
  static const Color btnPrimary = ink;
  static const Color btnPrimaryFg = night;

  static const String _font = 'Geist';

  static TextStyle? _scale(TextStyle? style, double factor) {
    if (style == null) return null;
    final size = style.fontSize;
    return size != null ? style.copyWith(fontSize: size * factor) : style;
  }

  static TextTheme _scaleTextTheme(TextTheme theme, double factor) {
    return TextTheme(
      displayLarge: _scale(theme.displayLarge, factor),
      displayMedium: _scale(theme.displayMedium, factor),
      displaySmall: _scale(theme.displaySmall, factor),
      headlineLarge: _scale(theme.headlineLarge, factor),
      headlineMedium: _scale(theme.headlineMedium, factor),
      headlineSmall: _scale(theme.headlineSmall, factor),
      titleLarge: _scale(theme.titleLarge, factor),
      titleMedium: _scale(theme.titleMedium, factor),
      titleSmall: _scale(theme.titleSmall, factor),
      bodyLarge: _scale(theme.bodyLarge, factor),
      bodyMedium: _scale(theme.bodyMedium, factor),
      bodySmall: _scale(theme.bodySmall, factor),
      labelLarge: _scale(theme.labelLarge, factor),
      labelMedium: _scale(theme.labelMedium, factor),
      labelSmall: _scale(theme.labelSmall, factor),
    );
  }

  static ThemeData dark() {
    final scheme = const ColorScheme.dark(
      primary: accent,
      onPrimary: btnPrimaryFg,
      secondary: warm,
      onSecondary: night,
      surface: surface,
      onSurface: ink,
      error: danger,
    );

    final base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      colorScheme: scheme,
      scaffoldBackgroundColor: night,
      fontFamily: _font,
      visualDensity: VisualDensity.comfortable,
    );

    final scaledText = _scaleTextTheme(base.textTheme, 1.0).copyWith(
      displaySmall: base.textTheme.displaySmall?.copyWith(
        fontFamily: _font,
        fontWeight: FontWeight.w600,
        letterSpacing: 0,
        color: ink,
        height: 1.0,
      ),
      headlineMedium: base.textTheme.headlineMedium?.copyWith(
        fontFamily: _font,
        fontWeight: FontWeight.w600,
        letterSpacing: 0,
        color: ink,
      ),
      headlineSmall: base.textTheme.headlineSmall?.copyWith(
        fontFamily: _font,
        fontWeight: FontWeight.w600,
        letterSpacing: 0,
        color: ink,
      ),
      titleLarge: base.textTheme.titleLarge?.copyWith(
        fontFamily: _font,
        fontWeight: FontWeight.w500,
        letterSpacing: 0,
        color: ink2,
      ),
      titleMedium: base.textTheme.titleMedium?.copyWith(
        fontFamily: _font,
        fontWeight: FontWeight.w400,
        letterSpacing: 0,
        color: ink2,
      ),
      bodyLarge: base.textTheme.bodyLarge?.copyWith(
        fontFamily: _font,
        color: ink2,
        letterSpacing: 0,
        height: 1.6,
      ),
      bodyMedium: base.textTheme.bodyMedium?.copyWith(
        fontFamily: _font,
        color: ink2,
        letterSpacing: 0,
      ),
      bodySmall: base.textTheme.bodySmall?.copyWith(
        fontFamily: _font,
        color: ink3,
        letterSpacing: 0,
      ),
      labelSmall: base.textTheme.labelSmall?.copyWith(
        fontFamily: _font,
        color: ink3,
        fontWeight: FontWeight.w500,
        letterSpacing: 1.2,
      ),
    );

    return base.copyWith(
      textTheme: scaledText,
      appBarTheme: const AppBarTheme(
        backgroundColor: night,
        elevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(
          fontFamily: _font,
          fontSize: 18,
          fontWeight: FontWeight.w700,
          letterSpacing: 0,
          color: ink,
        ),
      ),
      cardTheme: CardThemeData(
        color: glassBg,
        elevation: 0,
        margin: const EdgeInsets.symmetric(vertical: 6),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(10),
          side: const BorderSide(color: glassBorder, width: 1),
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: btnPrimary,
          foregroundColor: btnPrimaryFg,
          minimumSize: const Size.fromHeight(52),
          textStyle: const TextStyle(
            fontFamily: _font,
            fontSize: 15,
            fontWeight: FontWeight.w500,
            letterSpacing: 0,
          ),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
          elevation: 0,
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: ink2,
          textStyle: const TextStyle(
            fontFamily: _font,
            fontSize: 15,
            fontWeight: FontWeight.w400,
            letterSpacing: 0,
          ),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: const Color(0x0EF1F0E8),
        contentPadding: const EdgeInsets.symmetric(horizontal: 22, vertical: 18),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: glassBorder, width: 1),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: glassBorder, width: 1),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: accent, width: 1.5),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: danger, width: 1),
        ),
        focusedErrorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: danger, width: 1.5),
        ),
        hintStyle: const TextStyle(
          fontFamily: _font,
          color: Color(0x59F2F5FF), // 35% opacity
          fontSize: 15,
        ),
        prefixIconColor: ink3,
        suffixIconColor: ink3,
        errorStyle: const TextStyle(
          fontFamily: _font,
          fontSize: 12,
          color: danger,
        ),
      ),
      snackBarTheme: const SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: surface2,
        contentTextStyle: TextStyle(
          fontFamily: _font,
          fontSize: 15,
          color: ink,
        ),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(8)),
        ),
      ),
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: surface,
        indicatorColor: const Color(0x225BD6A6),
        labelTextStyle: WidgetStateProperty.all(
          const TextStyle(
            fontFamily: _font,
            fontSize: 12,
            fontWeight: FontWeight.w500,
            letterSpacing: 0,
            color: ink2,
          ),
        ),
        iconTheme: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.selected)) {
            return const IconThemeData(color: accent);
          }
          return const IconThemeData(color: ink3);
        }),
      ),
      dividerTheme: const DividerThemeData(
        color: glassBorder,
        thickness: 1,
      ),
      progressIndicatorTheme: const ProgressIndicatorThemeData(
        color: accent,
      ),
    );
  }
}
