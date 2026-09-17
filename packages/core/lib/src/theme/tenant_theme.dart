import 'package:flutter/material.dart';

/// The tenant's published theme tokens become a Flutter [ThemeData] at runtime.
///
/// This is the same document the web storefront turns into CSS variables, so a colour changed in
/// the page builder shows up in the app on the next launch — no rebuild, no store review.
class TenantTheme {
  static ThemeData fromTokens(Map<String, dynamic> tokens, {Brightness brightness = Brightness.light}) {
    final colors = Map<String, dynamic>.from(
      (brightness == Brightness.dark ? tokens['colors_dark'] : tokens['colors']) as Map? ??
          tokens['colors'] as Map? ??
          {},
    );
    final radius = _number(tokens['radius'], 12);
    final primary = _color(colors['primary'], const Color(0xFF0F766E));
    final scheme = ColorScheme.fromSeed(
      seedColor: primary,
      brightness: brightness,
    ).copyWith(
      primary: primary,
      onPrimary: _color(colors['primary_fg'], Colors.white),
      surface: _color(colors['surface'], brightness == Brightness.dark ? const Color(0xFF111827) : Colors.white),
      error: _color(colors['danger'], const Color(0xFFDC2626)),
    );
    final fontFamily = _fontFamily(tokens);
    return ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: scheme,
      scaffoldBackgroundColor: _color(colors['bg'], scheme.surface),
      fontFamily: fontFamily,
      appBarTheme: AppBarTheme(
        backgroundColor: _color(colors['surface'], scheme.surface),
        foregroundColor: _color(colors['fg'], scheme.onSurface),
        elevation: 0,
      ),
      cardTheme: CardTheme(
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(radius),
          side: BorderSide(color: _color(colors['border'], scheme.outlineVariant)),
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          minimumSize: const Size.fromHeight(48), // thumb-sized, one-handed
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(radius)),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(radius)),
        filled: true,
        fillColor: _color(colors['surface'], scheme.surface),
      ),
    );
  }

  /// Bangla needs a font that actually has the glyphs; the tenant may name its own.
  static String? _fontFamily(Map<String, dynamic> tokens) {
    final typography = tokens['typography'];
    if (typography is Map && typography['body_family'] is String) {
      return typography['body_family'] as String;
    }
    return null;
  }

  static Color _color(Object? value, Color fallback) {
    if (value is! String) return fallback;
    final hex = value.replaceAll('#', '').trim();
    if (hex.length == 6) return Color(int.parse('FF$hex', radix: 16));
    if (hex.length == 8) return Color(int.parse(hex, radix: 16));
    return fallback;
  }

  static double _number(Object? value, double fallback) {
    if (value is num) return value.toDouble();
    if (value is String) {
      final digits = value.replaceAll(RegExp(r'[^0-9.]'), '');
      return double.tryParse(digits) ?? fallback;
    }
    return fallback;
  }
}
