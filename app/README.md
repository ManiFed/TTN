# The Telescope Net — Desktop Member App (Flutter)

The desktop control surface for The Telescope Net automated telescope network.
It talks to both the background local node service (`http://127.0.0.1:5173`)
and the cloud member API (`cloud/server.py`, routes under `/api/v1/*`).

## What's here

```
lib/
  config.dart            API base URL (override with --dart-define=BS_API_BASE=...)
  main.dart              App entry + auth gate
  theme.dart             Dark theme (large type, high contrast)
  api/
    api_client.dart      Typed wrapper over the cloud member API
    auth_store.dart      Persists the bearer token (shared_preferences)
  models/models.dart     Member, Node, MemberStats, Observation, AppNotification
  state/app_state.dart   Session + member state (provider / ChangeNotifier)
  screens/
    login_screen.dart    Sign in / register
    home_screen.dart     Tab shell (NavigationBar)
    dashboard_tab.dart   "Tonight": cumulative member stats
    nodes_tab.dart       Telescopes list + claim-a-node flow
    observations_tab.dart Recent photometric measurements
    notifications_tab.dart Member alerts
  widgets/async_view.dart Loading / error / empty + pull-to-refresh helper
```

## Platforms

Desktop only — macOS, Windows, and Linux/Raspberry Pi OS. This is *the* member
app and the only place a member controls a telescope; it runs on the same
computer as the node agent and reaches it on `http://127.0.0.1:5173`.

iOS is deactivated: both the standalone SwiftUI app and this project's iOS build
target live in `archive/ios/` and are not built, shipped, or tested. Do not
re-add an `ios/` platform folder here without reading `archive/README.md` first —
the onboarding contract it was written against no longer exists.

## First-time setup

The Flutter SDK is **not** installed on the build machine yet, and this folder
holds only the Dart source (no `android/`, `ios/`, etc.). To turn it into a
runnable project:

```bash
# 1. Install Flutter: https://docs.flutter.dev/get-started/install
flutter --version          # confirm >= 3.27

# 2. Generate the committed desktop platform folders in place.
cd app
flutter create . --platforms=macos,windows,linux

# 3. Fetch dependencies
flutter pub get

# 4. Run against a local cloud (python -m cloud.main on :8800).
#    cloud/config.yaml expects PostgreSQL at:
#    postgresql://boundless@/boundless?host=/tmp
flutter run -d macos --dart-define=BS_API_BASE=http://localhost:8800
```

> For another computer on the LAN, use the cloud host's LAN IP instead of
> `localhost` (for example `--dart-define=BS_API_BASE=http://192.168.1.20:8800`).

Build release artifacts with `flutter build macos`, `flutter build windows`, or
`flutter build linux`. Raspberry Pi OS with a desktop environment uses the Linux
target; a separate embedder is not required.

## Shipping the unified macOS application

The macOS installer packages two applications together: the visible Flutter
window (`TelescopeNet.app`) and the hidden Python node agent
(`TelescopeNetNode.app`). The installer starts the latter as a launchd service
and opens the former for the logged-in user.

```bash
cd app
flutter create . --platforms=macos
# flutter_tts requires macOS 10.15 or newer.
sed -i '' "s/platform :osx, '10.14'/platform :osx, '10.15'/" macos/Podfile
sed -i '' 's/MACOSX_DEPLOYMENT_TARGET = 10.14/MACOSX_DEPLOYMENT_TARGET = 10.15/g' macos/Runner.xcodeproj/project.pbxproj
flutter build macos --release
cd ..
python3 build/build.py --skip-astap
bash build/macos/build_dmg.sh --sign 'Developer ID Application: Your Team'
```

The in-app Local Node screen is the migration path from the legacy browser
dashboard. The browser dashboard remains available only for controls that have
not yet been ported.

## Accessibility notes

- Enlarged default type scale (1.1×) and high-contrast night-sky palette.
- Touch targets ≥ 48 dp; buttons are full-width and 56 dp tall.
- Status is conveyed by **icon + text**, never colour alone.
- Every interactive element and stat is wrapped in `Semantics` for screen
  readers (TalkBack / VoiceOver).

## API contract

All endpoints are versioned under `/api/v1`. Auth is a bearer token issued by
`/auth/login` and `/auth/register`, sent as `Authorization: Bearer <token>`.
See `cloud/server.py` for the source of truth; keep `models/models.dart` in
sync when response shapes change.
