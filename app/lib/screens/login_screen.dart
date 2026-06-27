import 'dart:ui';
import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../state/app_state.dart';
import '../theme.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _email = TextEditingController();
  final _password = TextEditingController();
  final _name = TextEditingController();

  bool _registering = false;
  bool _busy = false;
  bool _obscure = true;

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    _name.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _busy = true);
    final state = context.read<AppState>();
    final ok = _registering
        ? await state.register(_email.text.trim(), _password.text, _name.text.trim())
        : await state.login(_email.text.trim(), _password.text);
    if (!mounted) return;
    setState(() => _busy = false);
    if (!ok && state.lastError != null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(state.lastError!)),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: kIsWeb ? Colors.transparent : BSTheme.night,
      body: Stack(
        children: [
          // Background: Aladin live sky on web, painted glow on native
          Positioned.fill(
            child: CustomPaint(painter: _NightGlowPainter()),
          ),
          // Brandmark
          const SafeArea(
            child: Padding(
              padding: EdgeInsets.symmetric(horizontal: 28, vertical: 20),
              child: Text(
                'The Telescope Net',
                style: TextStyle(
                  fontFamily: 'Geist',
                  fontWeight: FontWeight.w700,
                  fontSize: 17,
                  letterSpacing: -0.5,
                  color: BSTheme.ink,
                ),
              ),
            ),
          ),
          // Centered glass panel
          Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 80),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: _GlassPanel(
                  child: Padding(
                    padding: const EdgeInsets.all(36),
                    child: Form(
                      key: _formKey,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          // Kicker
                          const Text(
                            'MEMBER ACCESS',
                            style: TextStyle(
                              fontFamily: 'Geist',
                              fontSize: 11,
                              fontWeight: FontWeight.w500,
                              letterSpacing: 2.2,
                              color: BSTheme.accent,
                            ),
                          ),
                          const SizedBox(height: 14),
                          // Title
                          Semantics(
                            header: true,
                            child: Text(
                              _registering ? 'Create account.' : 'Welcome back.',
                              style: const TextStyle(
                                fontFamily: 'Geist',
                                fontSize: 34,
                                fontWeight: FontWeight.w600,
                                letterSpacing: -1.4,
                                color: BSTheme.ink,
                                height: 1.0,
                              ),
                            ),
                          ),
                          const SizedBox(height: 6),
                          Text(
                            _registering
                                ? 'Join the network.'
                                : 'Stargazer.',
                            style: const TextStyle(
                              fontFamily: 'Geist',
                              fontSize: 34,
                              fontWeight: FontWeight.w600,
                              letterSpacing: -1.4,
                              color: BSTheme.accent,
                              height: 1.0,
                            ),
                          ),
                          const SizedBox(height: 32),
                          // Fields
                          if (_registering) ...[
                            _PillField(
                              controller: _name,
                              hint: 'Display name',
                              icon: Icons.person_outline,
                              action: TextInputAction.next,
                              validator: (v) => (v == null || v.trim().isEmpty)
                                  ? 'Please enter a name'
                                  : null,
                            ),
                            const SizedBox(height: 12),
                          ],
                          _PillField(
                            controller: _email,
                            hint: 'Email',
                            icon: Icons.mail_outline,
                            keyboardType: TextInputType.emailAddress,
                            autofillHints: const [AutofillHints.email],
                            action: TextInputAction.next,
                            validator: (v) => (v == null || !v.contains('@'))
                                ? 'Enter a valid email'
                                : null,
                          ),
                          const SizedBox(height: 12),
                          _PillField(
                            controller: _password,
                            hint: 'Password',
                            icon: Icons.lock_outline,
                            obscureText: _obscure,
                            autofillHints: const [AutofillHints.password],
                            action: TextInputAction.done,
                            onSubmitted: (_) => _submit(),
                            suffixIcon: IconButton(
                              tooltip: _obscure ? 'Show password' : 'Hide password',
                              icon: Icon(
                                _obscure ? Icons.visibility_outlined : Icons.visibility_off_outlined,
                                size: 18,
                                color: BSTheme.ink3,
                              ),
                              onPressed: () => setState(() => _obscure = !_obscure),
                            ),
                            validator: (v) => (v == null || v.length < 8)
                                ? 'At least 8 characters'
                                : null,
                          ),
                          const SizedBox(height: 24),
                          // Primary button
                          _PrimaryButton(
                            onPressed: _busy ? null : _submit,
                            busy: _busy,
                            label: _registering ? 'Create account' : 'Sign in',
                          ),
                          const SizedBox(height: 16),
                          // Toggle link
                          Center(
                            child: GestureDetector(
                              onTap: _busy
                                  ? null
                                  : () => setState(() => _registering = !_registering),
                              child: Text(
                                _registering
                                    ? 'Already a member? Sign in'
                                    : 'New here? Create an account',
                                style: const TextStyle(
                                  fontFamily: 'Geist',
                                  fontSize: 14,
                                  color: BSTheme.ink2,
                                  decoration: TextDecoration.underline,
                                  decorationColor: BSTheme.ink3,
                                ),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Glass panel widget
// ---------------------------------------------------------------------------

class _GlassPanel extends StatelessWidget {
  const _GlassPanel({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(26),
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 14, sigmaY: 14),
        child: Container(
          decoration: BoxDecoration(
            color: const Color(0x0BA0B9FF), // rgba(160,185,255,.045)
            borderRadius: BorderRadius.circular(26),
            border: Border.all(color: BSTheme.glassBorder, width: 1),
            boxShadow: const [
              BoxShadow(
                color: Color(0x2800000A),
                blurRadius: 80,
                offset: Offset(0, 28),
              ),
            ],
          ),
          child: Stack(
            children: [
              // Top gloss highlight
              Positioned(
                top: 0,
                left: 0,
                right: 0,
                height: 80,
                child: Container(
                  decoration: BoxDecoration(
                    borderRadius: const BorderRadius.vertical(top: Radius.circular(26)),
                    gradient: LinearGradient(
                      begin: Alignment.topCenter,
                      end: Alignment.bottomCenter,
                      colors: [
                        Colors.white.withValues(alpha: 0.10),
                        Colors.white.withValues(alpha: 0.0),
                      ],
                    ),
                  ),
                ),
              ),
              child,
            ],
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Pill text field
// ---------------------------------------------------------------------------

class _PillField extends StatelessWidget {
  const _PillField({
    required this.controller,
    required this.hint,
    required this.icon,
    this.keyboardType,
    this.autofillHints,
    this.action = TextInputAction.next,
    this.onSubmitted,
    this.obscureText = false,
    this.suffixIcon,
    this.validator,
  });

  final TextEditingController controller;
  final String hint;
  final IconData icon;
  final TextInputType? keyboardType;
  final Iterable<String>? autofillHints;
  final TextInputAction action;
  final ValueChanged<String>? onSubmitted;
  final bool obscureText;
  final Widget? suffixIcon;
  final FormFieldValidator<String>? validator;

  @override
  Widget build(BuildContext context) {
    return TextFormField(
      controller: controller,
      keyboardType: keyboardType,
      autofillHints: autofillHints,
      textInputAction: action,
      onFieldSubmitted: onSubmitted,
      obscureText: obscureText,
      style: const TextStyle(
        fontFamily: 'Geist',
        fontSize: 15,
        color: BSTheme.ink,
        letterSpacing: -0.1,
      ),
      decoration: InputDecoration(
        hintText: hint,
        prefixIcon: Icon(icon, size: 18),
        suffixIcon: suffixIcon,
      ),
      validator: validator,
    );
  }
}

// ---------------------------------------------------------------------------
// Primary pill button
// ---------------------------------------------------------------------------

class _PrimaryButton extends StatelessWidget {
  const _PrimaryButton({
    required this.label,
    required this.onPressed,
    required this.busy,
  });

  final String label;
  final VoidCallback? onPressed;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 52,
      child: ElevatedButton(
        onPressed: onPressed,
        child: busy
            ? const SizedBox(
                height: 18,
                width: 18,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  color: BSTheme.btnPrimaryFg,
                ),
              )
            : Text(label),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Background painter — subtle radial glow
// ---------------------------------------------------------------------------

class _NightGlowPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    // Top-center blue glow
    final topGlow = RadialGradient(
      center: Alignment.topCenter,
      radius: 0.9,
      colors: [
        const Color(0xFF8FD9FF).withValues(alpha: 0.06),
        Colors.transparent,
      ],
    );
    canvas.drawRect(
      Offset.zero & size,
      Paint()..shader = topGlow.createShader(Offset.zero & size),
    );
    // Bottom-left warm glow
    final warmGlow = RadialGradient(
      center: const Alignment(-1.0, 1.2),
      radius: 0.8,
      colors: [
        const Color(0xFFFFC07A).withValues(alpha: 0.04),
        Colors.transparent,
      ],
    );
    canvas.drawRect(
      Offset.zero & size,
      Paint()..shader = warmGlow.createShader(Offset.zero & size),
    );
  }

  @override
  bool shouldRepaint(_NightGlowPainter old) => false;
}
