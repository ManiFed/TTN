# Boundless Skies — Member App (Flutter)

The accessible, disability-first mobile app for the Boundless Skies automated
telescope network. It talks to the cloud layer's member API (`cloud/server.py`,
routes under `/api/v1/*`).

## What's here

```
lib/
  config.dart            API base URL (override with --dart-define=BS_API_BASE=...)
  main.dart              App entry + auth gate
  theme.dart             Disability-first dark theme (large type, high contrast)
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

## First-time setup

The Flutter SDK is **not** installed on the build machine yet, and this folder
holds only the Dart source (no `android/`, `ios/`, etc.). To turn it into a
runnable project:

```bash
# 1. Install Flutter: https://docs.flutter.dev/get-started/install
flutter --version          # confirm >= 3.27

# 2. Generate the platform folders in place (keeps lib/, pubspec.yaml)
cd app
flutter create .

# 3. Fetch dependencies
flutter pub get

# 4. Run against a local cloud (python -m cloud.main on :8800)
flutter run --dart-define=BS_API_BASE=http://localhost:8800
```

> On a physical phone, `localhost` points at the phone. Use your computer's LAN
> IP (e.g. `--dart-define=BS_API_BASE=http://192.168.1.20:8800`). Android also
> needs cleartext HTTP allowed for local testing, or use HTTPS in production.

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
