import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api/api_client.dart';
import '../state/app_state.dart';
import '../theme.dart';

/// Form for members to propose a new science program for the network.
class SuggestProgramScreen extends StatefulWidget {
  const SuggestProgramScreen({super.key});

  @override
  State<SuggestProgramScreen> createState() => _SuggestProgramScreenState();
}

class _SuggestProgramScreenState extends State<SuggestProgramScreen> {
  final _formKey = GlobalKey<FormState>();
  final _title = TextEditingController();
  final _description = TextEditingController();
  final _targets = TextEditingController();
  final _notes = TextEditingController();

  bool _submitting = false;
  bool _submitted = false;

  @override
  void dispose() {
    _title.dispose();
    _description.dispose();
    _targets.dispose();
    _notes.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _submitting = true;
    });
    final api = context.read<AppState>().api;
    try {
      await api.suggestScienceProgram(
        title: _title.text.trim(),
        description: _description.text.trim(),
        targetExamples: _targets.text.trim(),
        notes: _notes.text.trim(),
      );
      if (!mounted) return;
      setState(() {
        _submitted = true;
        _submitting = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _submitting = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(e.message)),
      );
    } catch (e) {
      if (!mounted) return;
      setState(() => _submitting = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Could not submit: $e')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: BSTheme.night,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        elevation: 0,
        title: const Text(
          'Suggest a program',
          style: TextStyle(
            fontFamily: 'Geist',
            fontSize: 16,
            fontWeight: FontWeight.w800,
            color: BSTheme.ink,
          ),
        ),
        iconTheme: const IconThemeData(color: BSTheme.ink2),
      ),
      body: _submitted ? _buildSuccess() : _buildForm(),
    );
  }

  Widget _buildSuccess() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 64,
              height: 64,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: BSTheme.success.withValues(alpha: 0.12),
                border: Border.all(color: BSTheme.success.withValues(alpha: 0.35)),
              ),
              child: const Icon(Icons.check_rounded, color: BSTheme.success, size: 32),
            ),
            const SizedBox(height: 20),
            const Text(
              'Suggestion received',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 22,
                fontWeight: FontWeight.w800,
                color: BSTheme.ink,
              ),
            ),
            const SizedBox(height: 10),
            const Text(
              'Thanks — our team will review your science program idea.',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontFamily: 'Geist',
                fontSize: 14,
                color: BSTheme.ink2,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 28),
            FilledButton(
              onPressed: () => Navigator.pop(context),
              style: FilledButton.styleFrom(
                backgroundColor: BSTheme.btnPrimary,
                foregroundColor: BSTheme.btnPrimaryFg,
                padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 14),
              ),
              child: const Text('Done'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildForm() {
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
      children: [
        const Text(
          'Tell us what the network should observe and why it matters scientifically.',
          style: TextStyle(
            fontFamily: 'Geist',
            fontSize: 14,
            color: BSTheme.ink2,
            height: 1.5,
          ),
        ),
        const SizedBox(height: 20),
        Form(
          key: _formKey,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              _field(
                controller: _title,
                label: 'Program name',
                hint: 'e.g. Dwarf nova monitoring',
                maxLines: 1,
                validator: (v) =>
                    (v == null || v.trim().isEmpty) ? 'Required' : null,
              ),
              const SizedBox(height: 14),
              _field(
                controller: _description,
                label: 'Scientific rationale',
                hint: 'What should be observed, how often, and what question does it answer?',
                maxLines: 5,
                validator: (v) =>
                    (v == null || v.trim().length < 20)
                        ? 'Please add at least a few sentences'
                        : null,
              ),
              const SizedBox(height: 14),
              _field(
                controller: _targets,
                label: 'Example targets (optional)',
                hint: 'Object names, catalogs, or sky regions',
                maxLines: 3,
              ),
              const SizedBox(height: 14),
              _field(
                controller: _notes,
                label: 'Additional notes (optional)',
                hint: 'Filters, cadence, coordination with other surveys…',
                maxLines: 3,
              ),
              const SizedBox(height: 24),
              FilledButton(
                onPressed: _submitting ? null : _submit,
                style: FilledButton.styleFrom(
                  backgroundColor: BSTheme.btnPrimary,
                  foregroundColor: BSTheme.btnPrimaryFg,
                  padding: const EdgeInsets.symmetric(vertical: 15),
                  textStyle: const TextStyle(
                    fontFamily: 'Geist',
                    fontSize: 15,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                child: _submitting
                    ? const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('Submit suggestion'),
              ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _field({
    required TextEditingController controller,
    required String label,
    required String hint,
    int maxLines = 1,
    String? Function(String?)? validator,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label.toUpperCase(),
          style: const TextStyle(
            fontFamily: 'Geist',
            fontSize: 10,
            fontWeight: FontWeight.w600,
            letterSpacing: 0.8,
            color: BSTheme.ink3,
          ),
        ),
        const SizedBox(height: 6),
        TextFormField(
          controller: controller,
          maxLines: maxLines,
          validator: validator,
          style: const TextStyle(
            fontFamily: 'Geist',
            fontSize: 14,
            color: BSTheme.ink,
          ),
          decoration: InputDecoration(
            hintText: hint,
            hintStyle: const TextStyle(color: BSTheme.ink3),
            filled: true,
            fillColor: BSTheme.surface.withValues(alpha: 0.88),
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10),
              borderSide: const BorderSide(color: BSTheme.glassBorder),
            ),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10),
              borderSide: const BorderSide(color: BSTheme.glassBorder),
            ),
            focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(10),
              borderSide: BorderSide(color: BSTheme.accent.withValues(alpha: 0.6)),
            ),
            contentPadding: const EdgeInsets.all(14),
          ),
        ),
      ],
    );
  }
}