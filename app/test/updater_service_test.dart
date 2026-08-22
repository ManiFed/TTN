import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:boundless_skies/services/updater_service.dart';

/// Replacing a running application is the one step here that can leave a
/// member with no working install at all. The rollback branch only runs when
/// something has already gone wrong — disk full, permissions changing
/// mid-flight — which is precisely when nobody is watching.
///
/// So these tests execute the real script against throwaway directories rather
/// than asserting on its text. `open` is shadowed by a stub on PATH so the test
/// does not try to launch anything.
void main() {
  // The updater is macOS-only by design -- downloadVerifyAndRestart throws
  // immediately anywhere else -- and the script it generates uses `ditto`,
  // which does not exist on Linux or Windows. Running these there tests the
  // absence of a macOS tool, not this code.
  final notMacOS = Platform.isMacOS ? null : 'macOS-only';

  late Directory root;
  late Directory binDir;

  setUp(() async {
    root = await Directory.systemTemp.createTemp('updater_test_');
    binDir = Directory('${root.path}/bin')..createSync();
    // A no-op `open`, so running the script cannot launch a real application.
    final openStub = File('${binDir.path}/open')
      ..writeAsStringSync('#!/bin/bash\nexit 0\n');
    await Process.run('chmod', ['+x', openStub.path]);
  });

  tearDown(() async {
    if (root.existsSync()) await root.delete(recursive: true);
  });

  /// Runs the generated script and returns its exit code.
  Future<int> runScript({
    required String target,
    required String newApp,
    required String workDir,
  }) async {
    final script = File('$workDir/apply.sh');
    script.writeAsStringSync(UpdaterService.buildApplyScript(
      targetPath: target,
      newAppPath: newApp,
      workDirPath: workDir,
      // Our own PID would make the script wait ~10s for us to exit; a PID that
      // is already gone lets the wait loop break immediately.
      waitForPid: 999999,
    ));
    await Process.run('chmod', ['+x', script.path]);
    final result = await Process.run(
      '/bin/bash',
      [script.path],
      environment: {'PATH': '${binDir.path}:${Platform.environment['PATH']}'},
    );
    return result.exitCode;
  }

  Directory makeBundle(String path, String marker) {
    final dir = Directory(path)..createSync(recursive: true);
    File('${dir.path}/version.txt').writeAsStringSync(marker);
    return dir;
  }

  test('a good update replaces the installed bundle', () async {
    final target = makeBundle('${root.path}/TelescopeNet.app', 'old');
    final newApp = makeBundle('${root.path}/new/TelescopeNet.app', 'new');
    final workDir = Directory('${root.path}/work')..createSync();

    await runScript(
        target: target.path, newApp: newApp.path, workDir: workDir.path);

    expect(File('${target.path}/version.txt').readAsStringSync(), 'new',
        reason: 'the new bundle should be in place');
    expect(Directory('${target.path}.bak').existsSync(), isFalse,
        reason: 'the backup should be cleaned up after a successful swap');
  }, skip: notMacOS);

  test('a failed copy restores the previous install', () async {
    // The single most important behaviour here: if the swap fails, the member
    // must end up back on the version they had, not with nothing.
    final target = makeBundle('${root.path}/TelescopeNet.app', 'old');
    final workDir = Directory('${root.path}/work')..createSync();

    await runScript(
      target: target.path,
      newApp: '${root.path}/does-not-exist.app', // ditto will fail
      workDir: workDir.path,
    );

    expect(target.existsSync(), isTrue,
        reason: 'the installed app must not be left missing');
    expect(File('${target.path}/version.txt').readAsStringSync(), 'old',
        reason: 'the previous version should be restored');
    expect(Directory('${target.path}.bak').existsSync(), isFalse,
        reason: 'the backup should be moved back, not left behind');
  }, skip: notMacOS);

  test('a stale backup from an earlier attempt does not block the swap',
      () async {
    // An update interrupted half-way leaves a .bak behind; the next attempt
    // must not trip over it.
    final target = makeBundle('${root.path}/TelescopeNet.app', 'old');
    makeBundle('${root.path}/TelescopeNet.app.bak', 'stale');
    final newApp = makeBundle('${root.path}/new/TelescopeNet.app', 'new');
    final workDir = Directory('${root.path}/work')..createSync();

    await runScript(
        target: target.path, newApp: newApp.path, workDir: workDir.path);

    expect(File('${target.path}/version.txt').readAsStringSync(), 'new');
    expect(Directory('${target.path}.bak').existsSync(), isFalse);
  }, skip: notMacOS);

  test('the working directory is cleaned up afterwards', () async {
    final target = makeBundle('${root.path}/TelescopeNet.app', 'old');
    final newApp = makeBundle('${root.path}/new/TelescopeNet.app', 'new');
    final workDir = Directory('${root.path}/work')..createSync();

    await runScript(
        target: target.path, newApp: newApp.path, workDir: workDir.path);

    expect(workDir.existsSync(), isFalse,
        reason: 'the downloaded update should not be left on disk');
  }, skip: notMacOS);

  test('a path containing spaces is handled', () async {
    // /Applications/My Telescope.app is entirely legal, and an unquoted
    // variable here would delete the wrong thing.
    final target = makeBundle('${root.path}/My Telescope.app', 'old');
    final newApp = makeBundle('${root.path}/new dir/My Telescope.app', 'new');
    final workDir = Directory('${root.path}/work space')..createSync();

    await runScript(
        target: target.path, newApp: newApp.path, workDir: workDir.path);

    expect(File('${target.path}/version.txt').readAsStringSync(), 'new');
  }, skip: notMacOS);

  test('the script waits for the old process to exit before swapping', () {
    final script = UpdaterService.buildApplyScript(
      targetPath: '/Applications/TelescopeNet.app',
      newAppPath: '/tmp/new.app',
      workDirPath: '/tmp/work',
      waitForPid: 4242,
    );
    // The swap cannot happen while the app being replaced is still running.
    expect(script, contains('kill -0 4242'));
    expect(script.indexOf('kill -0 4242'), lessThan(script.indexOf('ditto')));
  });
}
