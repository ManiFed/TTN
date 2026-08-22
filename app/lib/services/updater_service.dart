import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart' show visibleForTesting;
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';

/// Self-updates the running macOS app in place: downloads the new .app
/// bundle, verifies it two ways (checksum against what the cloud published,
/// then code-signature verification of the extracted bundle), swaps it into
/// this app's install location, and relaunches — replacing the old
/// open-a-browser-tab update flow with "quit and come back on the new build".
///
/// macOS only. Every other platform (or a dev build not running from a real
/// .app bundle) throws immediately so the caller can fall back to the manual
/// download-page banner instead of silently doing nothing.
class UpdaterService {
  UpdaterService._();

  static Future<void> downloadVerifyAndRestart({
    required String zipUrl,
    required String expectedSha256,
  }) async {
    if (!Platform.isMacOS) {
      throw StateError('Self-update is only implemented for macOS.');
    }
    final bundle = _currentAppBundle();
    if (bundle == null) {
      throw StateError(
          'Not running from a .app bundle — cannot self-update (dev build?).');
    }
    if (!await _canWrite(bundle.parent)) {
      throw StateError(
          'No write access to ${bundle.parent.path} — cannot self-update.');
    }

    final tmpRoot = await getTemporaryDirectory();
    final workDir = await tmpRoot.createTemp('ttn_update_');
    try {
      final zipFile = File('${workDir.path}/update.zip');
      final res = await http
          .get(Uri.parse(zipUrl))
          .timeout(const Duration(minutes: 5));
      if (res.statusCode != 200) {
        throw StateError('Update download failed (HTTP ${res.statusCode}).');
      }
      await zipFile.writeAsBytes(res.bodyBytes);

      final digest = sha256.convert(await zipFile.readAsBytes()).toString();
      if (digest.toLowerCase() != expectedSha256.toLowerCase()) {
        throw StateError('Update checksum mismatch — refusing to install.');
      }

      // `ditto` (bundled with macOS) preserves resource forks/xattrs/code
      // signatures on extraction, unlike `unzip`.
      final extractDir = Directory('${workDir.path}/extracted');
      await extractDir.create();
      final extract = await Process.run(
          'ditto', ['-x', '-k', zipFile.path, extractDir.path]);
      if (extract.exitCode != 0) {
        throw StateError('Could not extract update: ${extract.stderr}');
      }

      final extractedApps = extractDir
          .listSync()
          .whereType<Directory>()
          .where((d) => d.path.endsWith('.app'))
          .toList();
      if (extractedApps.isEmpty) {
        throw StateError('Update archive did not contain an .app bundle.');
      }
      final newApp = extractedApps.first;

      // Belt-and-suspenders: verify the extracted bundle's code signature
      // before it can ever be launched, so a corrupted or tampered release
      // asset can't run even if it somehow matched the published checksum.
      final verify = await Process.run(
          'codesign', ['--verify', '--deep', '--strict', newApp.path]);
      if (verify.exitCode != 0) {
        throw StateError(
            'Update failed code-signature verification: ${verify.stderr}');
      }

      await _stageRelaunchAndQuit(currentBundle: bundle, newApp: newApp, workDir: workDir);
    } catch (_) {
      await workDir.delete(recursive: true).catchError((_) => workDir);
      rethrow;
    }
  }

  /// Writes a small shell script that waits for this process to exit, swaps
  /// the new bundle into place, and reopens it — then launches it detached
  /// and quits. The swap can't happen from inside this process because the
  /// running executable is inside the very directory being replaced.
  static Future<void> _stageRelaunchAndQuit({
    required Directory currentBundle,
    required Directory newApp,
    required Directory workDir,
  }) async {
    final script = File('${workDir.path}/apply_update.sh');
    await script.writeAsString(buildApplyScript(
      targetPath: currentBundle.path,
      newAppPath: newApp.path,
      workDirPath: workDir.path,
      waitForPid: pid,
    ));
    await Process.run('chmod', ['+x', script.path]);
    await Process.start('/bin/bash', [script.path],
        mode: ProcessStartMode.detached);
    exit(0);
  }

  /// The script that actually replaces the installed bundle.
  ///
  /// Extracted so it can be executed against a throwaway directory in tests:
  /// this is the one step that can destroy a working install, and the rollback
  /// branch only ever runs when something has already gone wrong — which is
  /// exactly when nobody is watching. @visibleForTesting rather than private
  /// for that reason.
  ///
  /// The old bundle is moved aside rather than deleted up front, so a partial
  /// `ditto` (disk full, permissions changing mid-flight) leaves the previous
  /// install restorable instead of gone.
  @visibleForTesting
  static String buildApplyScript({
    required String targetPath,
    required String newAppPath,
    required String workDirPath,
    required int waitForPid,
  }) =>
      '''
#!/bin/bash
for i in \$(seq 1 100); do
  kill -0 $waitForPid 2>/dev/null || break
  sleep 0.1
done
BACKUP="$targetPath.bak"
rm -rf "\$BACKUP"
mv "$targetPath" "\$BACKUP"
if ditto "$newAppPath" "$targetPath"; then
  rm -rf "\$BACKUP"
else
  rm -rf "$targetPath"
  mv "\$BACKUP" "$targetPath"
fi
open "$targetPath"
rm -rf "$workDirPath"
''';

  /// Finds the `.app` bundle directory containing the running executable
  /// (e.g. `/Applications/TelescopeNet.app`), or null if this isn't a real
  /// installed bundle.
  static Directory? _currentAppBundle() {
    var dir = File(Platform.resolvedExecutable).parent;
    for (var i = 0; i < 6; i++) {
      if (dir.path.endsWith('.app')) return dir;
      final parent = dir.parent;
      if (parent.path == dir.path) return null;
      dir = parent;
    }
    return null;
  }

  static Future<bool> _canWrite(Directory dir) async {
    try {
      final probe = File(
          '${dir.path}/.ttn_update_write_test_${DateTime.now().microsecondsSinceEpoch}');
      await probe.writeAsString('x');
      await probe.delete();
      return true;
    } catch (_) {
      return false;
    }
  }
}
