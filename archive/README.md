# Archive — deactivated code, kept for reference only

Nothing in this directory is built, shipped, tested, or deployed. No CI
workflow references it, and no runtime code imports it. It is kept so the
work isn't lost and so history stays readable — not because it still runs.

## `ios/` — the iOS member apps (deactivated 2026-07-19)

**The desktop app (`app/`) is now the only member application.** It is the
single place to control a telescope, on macOS, Windows, and Linux/Raspberry
Pi OS.

Two things moved here:

| Was | Now | What it was |
|---|---|---|
| `app/ios-native/` | `ios/ios-native/` | Standalone SwiftUI iOS member app |
| `app/ios/` | `ios/flutter-ios-runner/` | Flutter's iOS build target for `app/` |

### Why

Beyond the decision to focus on desktop, the iOS app could not link a
telescope at all. Its "Connect node" flow was built entirely on activation
codes, which the cloud retired: `POST /api/v1/me/activation-code` returns
**410 Gone**, and its pairing request sent `{pairing_token, activation_code}`
to an endpoint that requires `{pairing_token, node_id, api_key}` — so every
attempt failed. It also told people to paste codes into a page that did not
exist. Rather than rebuild that flow for a platform being dropped, the app
was deactivated and the desktop path was fixed instead (see the node setup
page at `http://localhost:5173` and `tests/fuzz/test_onboarding.py`).

The Flutter iOS runner went with it: `app/README.md` has always described
this project as desktop-only (`flutter create . --platforms=macos,windows,linux`),
so the `ios/` platform folder was scaffolding that could still produce an
iOS build carrying the same broken onboarding.

### If iOS is ever revived

Neither directory can be restored by copying it back alone — the pairing
contract it targets no longer exists. Any revival needs the current flow:
`POST /api/v1/me/nodes/attach` to get `{node_id, api_key}`, then
`POST /api/v1/nodes/pair` with `{pairing_token, node_id, api_key}` using the
pairing code shown on the node's setup page.

```bash
# Flutter iOS target: regenerate rather than restore, then re-apply any
# signing/Firebase configuration from archive/ios/flutter-ios-runner/.
cd app && flutter create . --platforms=ios
```
