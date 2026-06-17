// Firebase configuration for Boundless Skies.
//
// To fill in real values:
//   1. Create a Firebase project at https://console.firebase.google.com
//   2. Add Android and iOS apps to the project
//   3. Install the FlutterFire CLI:  dart pub global activate flutterfire_cli
//   4. Run: flutterfire configure --project=<your-firebase-project-id>
//      This overwrites this file with real keys.
//
// For Android, also add the google-services Gradle plugin to:
//   android/build.gradle  (classpath)
//   android/app/build.gradle  (apply plugin)
//
// Until configured, the app initialises without Firebase and push
// notifications are silently disabled (see push_service.dart).

import 'package:firebase_core/firebase_core.dart' show FirebaseOptions;
import 'package:flutter/foundation.dart'
    show TargetPlatform, defaultTargetPlatform, kIsWeb;

class DefaultFirebaseOptions {
  static FirebaseOptions get currentPlatform {
    if (kIsWeb) return web;
    switch (defaultTargetPlatform) {
      case TargetPlatform.android:
        return android;
      case TargetPlatform.iOS:
        return ios;
      case TargetPlatform.macOS:
        return macos;
      default:
        throw UnsupportedError(
          'Boundless Skies has no Firebase config for '
          '${defaultTargetPlatform.name}. Run flutterfire configure.',
        );
    }
  }

  // TODO: replace all fields below with values from flutterfire configure.
  static const FirebaseOptions android = FirebaseOptions(
    apiKey: 'TODO-android-api-key',
    appId: '1:000000000000:android:0000000000000000',
    messagingSenderId: '000000000000',
    projectId: 'TODO-firebase-project-id',
    storageBucket: 'TODO-firebase-project-id.appspot.com',
  );

  static const FirebaseOptions ios = FirebaseOptions(
    apiKey: 'TODO-ios-api-key',
    appId: '1:000000000000:ios:0000000000000000',
    messagingSenderId: '000000000000',
    projectId: 'TODO-firebase-project-id',
    storageBucket: 'TODO-firebase-project-id.appspot.com',
    iosBundleId: 'org.boundlessskies.app',
  );

  static const FirebaseOptions macos = FirebaseOptions(
    apiKey: 'TODO-macos-api-key',
    appId: '1:000000000000:ios:0000000000000000',
    messagingSenderId: '000000000000',
    projectId: 'TODO-firebase-project-id',
    storageBucket: 'TODO-firebase-project-id.appspot.com',
    iosBundleId: 'org.boundlessskies.app',
  );

  static const FirebaseOptions web = FirebaseOptions(
    apiKey: 'TODO-web-api-key',
    appId: '1:000000000000:web:0000000000000000',
    messagingSenderId: '000000000000',
    projectId: 'TODO-firebase-project-id',
    storageBucket: 'TODO-firebase-project-id.appspot.com',
    authDomain: 'TODO-firebase-project-id.firebaseapp.com',
  );
}
