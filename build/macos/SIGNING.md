# Signing and notarizing the macOS installer

Without this, Gatekeeper shows "Apple could not verify TelescopeNetNode-macOS.pkg
is free of malware" and offers only Move to Trash. Fixing it takes two Apple
certificates and one API key -- the release pipeline does the rest.

Your existing "Apple Distribution" certificates are for the App Store and do
**not** satisfy Gatekeeper for downloads. You need the **Developer ID** pair.

## One-time setup (Apple Developer account holder)

1. Xcode -> Settings -> Accounts -> your team -> **Manage Certificates** -> `+`
   -> create **Developer ID Application**, then **Developer ID Installer**.
   (Or developer.apple.com -> Certificates. Requires the Account Holder role.)
2. Keychain Access -> My Certificates: select both new certs (with their private
   keys) -> right-click -> **Export 2 items...** -> `certs.p12`, pick a password.
3. App Store Connect -> Users and Access -> Integrations -> **App Store Connect
   API** -> generate a key (Developer access). Download `AuthKey_XXXX.p8`, note
   the **Key ID** and the **Issuer ID**.
4. Add these GitHub repo secrets (Settings -> Secrets and variables -> Actions):

   | Secret | Value |
   |---|---|
   | `MACOS_CERTS_P12_BASE64` | `base64 -i certs.p12 \| pbcopy` |
   | `MACOS_CERTS_P12_PASSWORD` | the password from step 2 |
   | `APPLE_API_KEY_P8` | full contents of the `.p8` file |
   | `APPLE_API_KEY_ID` | Key ID |
   | `APPLE_API_ISSUER` | Issuer ID |

Next push to `main` (or a manual `Release macOS installer` run) produces a
signed, notarized, stapled pkg. If the secrets are missing the release still
ships, unsigned, with a workflow warning.

## Building locally

```bash
# store notary credentials once
xcrun notarytool store-credentials ttn-notary --key AuthKey_XXXX.p8 --key-id KEYID --issuer ISSUER

CODESIGN_IDENTITY="Developer ID Application: Name (TEAMID)" \
  python build/build.py --bundle-only --clean
APP_SIGN_ID="Developer ID Application: Name (TEAMID)" \
PKG_SIGN_ID="Developer ID Installer: Name (TEAMID)" \
NOTARY_PROFILE=ttn-notary \
  bash build/macos/build_dmg.sh
```

## Verifying a download

```bash
spctl --assess --type install --verbose=2 TelescopeNetNode-macOS.pkg   # accepted, source=Notarized Developer ID
xcrun stapler validate TelescopeNetNode-macOS.pkg
```

Entitlements for the hardened runtime live in `entitlements.plist` (PyInstaller
one-file apps need unsigned-executable-memory and disabled library validation).
