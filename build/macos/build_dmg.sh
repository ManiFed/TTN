#!/bin/bash
# The Telescope Net Node Agent — macOS pkg / dmg builder
#
# Usage:  bash build/macos/build_dmg.sh [--sign "Developer ID Application: ..."]
#
# Prerequisites:
#   PyInstaller bundle already built at dist/TelescopeNetNode
#   Flutter desktop bundle built at app/build/macos/Build/Products/Release/boundless_skies.app
#   pkgbuild + productbuild  (Xcode command-line tools)
#
# Outputs:
#   dist/TelescopeNetNode-X.Y.Z-macOS.pkg   (GUI installer)
#
# Signing + notarization (what stops Gatekeeper's "Apple could not verify..."
# warning) is driven by environment variables, all optional:
#   APP_SIGN_ID       "Developer ID Application: Name (TEAMID)" -- signs the .app
#                     (also accepted as `--sign`)
#   PKG_SIGN_ID       "Developer ID Installer: Name (TEAMID)"   -- signs the .pkg
#   NOTARY_PROFILE    notarytool keychain profile (local use), OR
#   APPLE_API_KEY_PATH / APPLE_API_KEY_ID / APPLE_API_ISSUER  (CI: App Store
#                     Connect API key .p8) -- submits the .pkg and staples it
# With none set the pkg is built unsigned; the distribution XML must still be
# self-contained so Installer.app can open it (no license/background references
# to files that are not shipped under build/macos/resources/).
# See build/macos/SIGNING.md.

set -e
cd "$(dirname "$0")/../.."   # repo root

VERSION="${VERSION:-1.0.4}"
APP_NAME="TelescopeNetNode"
BUNDLE_DIR="dist/${APP_NAME}.app"
CONTENTS="${BUNDLE_DIR}/Contents"
MACOS_DIR="${CONTENTS}/MacOS"
RESOURCES_DIR="${CONTENTS}/Resources"
BUILD_DIR="build/macos"
DIST_DIR="dist"

SIGN_ID="${APP_SIGN_ID:-}"
if [ "$1" = "--sign" ]; then
    SIGN_ID="$2"
fi
PKG_SIGN_ID="${PKG_SIGN_ID:-}"
ENTITLEMENTS="${BUILD_DIR}/entitlements.plist"

echo "=== Building The Telescope Net Node Agent for macOS v${VERSION} ==="

# ── Guard: require the PyInstaller bundle ──────────────────────────────────────
if [ ! -f "${DIST_DIR}/${APP_NAME}" ]; then
    echo "ERROR: PyInstaller bundle not found at ${DIST_DIR}/${APP_NAME}"
    echo "Run first:  python -m PyInstaller build/node_agent.spec --clean --noconfirm"
    exit 1
fi
# ── Assemble background agent .app ─────────────────────────────────────────────
echo "Assembling .app bundle..."
rm -rf "${BUNDLE_DIR}"
mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}"

cp "${DIST_DIR}/${APP_NAME}" "${MACOS_DIR}/${APP_NAME}"
chmod +x "${MACOS_DIR}/${APP_NAME}"

cat > "${CONTENTS}/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>org.telescopenet.nodeagent</string>
    <key>CFBundleName</key>
    <string>The Telescope Net Node Agent</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <!-- Agent: no Dock icon. Double-click opens TelescopeNet.app and exits. -->
    <key>LSUIElement</key>
    <true/>
    <key>LSBackgroundOnly</key>
    <false/>
    <key>CFBundleGetInfoString</key>
    <string>The Telescope Net Node Agent — background service; opens The Telescope Net app</string>
</dict>
</plist>
EOF

cp "${BUILD_DIR}/com.boundlessskies.nodeagent.plist" "${RESOURCES_DIR}/com.telescopenet.nodeagent.plist"
cp "build/config.template.yaml" "${RESOURCES_DIR}/"
[ -f "build/icon.icns" ] && cp "build/icon.icns" "${RESOURCES_DIR}/AppIcon.icns"

# ── No foreground app ─────────────────────────────────────────────────────────
# The Flutter member app is retired. The interface is the chat page the node
# agent serves at localhost:5173/chat, plus whichever AI assistant the member
# already uses -- postinstall.sh registers the telescope with all of them.
#
# Shipping the app alongside that would put a second, diverging control surface
# in /Applications and hand new members the one we are moving away from.

# ── Code signing ───────────────────────────────────────────────────────────────
# Hardened runtime + secure timestamp are both required for notarization. The
# PyInstaller binary's embedded libraries were already signed at build time
# (CODESIGN_IDENTITY in node_agent.spec); this signs the main executable and
# the bundle around it. No --deep: Apple discourages it and it re-signs badly.
if [ -n "${SIGN_ID}" ]; then
    echo "Code-signing with: ${SIGN_ID}"
    codesign --force --options runtime --timestamp \
        --entitlements "${ENTITLEMENTS}" \
        --sign "${SIGN_ID}" \
        "${MACOS_DIR}/${APP_NAME}"
    codesign --force --options runtime --timestamp \
        --entitlements "${ENTITLEMENTS}" \
        --sign "${SIGN_ID}" \
        "${BUNDLE_DIR}"
    codesign --verify --deep --strict --verbose=2 "${BUNDLE_DIR}"
else
    echo "Skipping code signing (set APP_SIGN_ID or pass --sign 'Developer ID Application: ...')"
fi

# ── Build component .pkg ───────────────────────────────────────────────────────
echo "Building .pkg installer..."
PKG_STAGING="${DIST_DIR}/pkg_staging"
COMPONENT_PKG="${DIST_DIR}/${APP_NAME}-${VERSION}-macOS-component.pkg"
FINAL_PKG="${DIST_DIR}/${APP_NAME}-${VERSION}-macOS.pkg"

rm -rf "${PKG_STAGING}"
mkdir -p "${PKG_STAGING}/Applications"
cp -r "${BUNDLE_DIR}" "${PKG_STAGING}/Applications/"

# pkgbuild only runs scripts named exactly "preinstall" / "postinstall"
# (no .sh extension). Stage copies so the repo can keep the .sh names.
SCRIPTS_STAGING="${DIST_DIR}/pkg_scripts"
rm -rf "${SCRIPTS_STAGING}"
mkdir -p "${SCRIPTS_STAGING}"
cp "${BUILD_DIR}/preinstall.sh" "${SCRIPTS_STAGING}/preinstall"
chmod 755 "${SCRIPTS_STAGING}/preinstall"
cp "${BUILD_DIR}/postinstall.sh" "${SCRIPTS_STAGING}/postinstall"
chmod 755 "${SCRIPTS_STAGING}/postinstall"

pkgbuild \
    --root "${PKG_STAGING}" \
    --identifier "org.telescopenet.nodeagent" \
    --version "${VERSION}" \
    --scripts "${SCRIPTS_STAGING}" \
    --install-location "/" \
    "${COMPONENT_PKG}"

# ── Build GUI installer .pkg via productbuild ──────────────────────────────────
# Checked-in installer UI resources. Only ship files we actually reference in
# distribution.xml — productbuild will happily embed a Resources tree that
# Installer.app then fails to load when the XML points at a missing license or
# background (see #118: "Could not load resource license: (null)", "Failed to
# load specified background image").
RESOURCES_SRC="${BUILD_DIR}/resources"
RESOURCES_STAGING="${DIST_DIR}/pkg_resources"
rm -rf "${RESOURCES_STAGING}"
mkdir -p "${RESOURCES_STAGING}"

for required in welcome.html conclusion.html; do
    if [ ! -f "${RESOURCES_SRC}/${required}" ]; then
        echo "ERROR: ${required} not found at ${RESOURCES_SRC}/${required}"
        echo "distribution.xml references this file; refusing to build a broken installer."
        exit 1
    fi
    cp "${RESOURCES_SRC}/${required}" "${RESOURCES_STAGING}/${required}"
done

# Intentionally omit <license> and <background>: build/macos/resources/ has
# welcome.html + conclusion.html only. Do not reintroduce those tags unless the
# matching files are added here and copied into RESOURCES_STAGING above.

cat > "${DIST_DIR}/distribution.xml" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
    <title>The Telescope Net ${VERSION}</title>
    <welcome file="welcome.html" mime-type="text/html"/>
    <conclusion file="conclusion.html" mime-type="text/html"/>
    <options customize="never" require-scripts="true" rootVolumeOnly="true"/>
    <choices-outline>
        <line choice="default">
            <line choice="org.telescopenet.nodeagent"/>
        </line>
    </choices-outline>
    <choice id="default"/>
    <choice id="org.telescopenet.nodeagent" visible="false">
        <pkg-ref id="org.telescopenet.nodeagent"/>
    </choice>
    <pkg-ref id="org.telescopenet.nodeagent" version="${VERSION}" onConclusion="none">
        ${APP_NAME}-${VERSION}-macOS-component.pkg
    </pkg-ref>
</installer-gui-script>
EOF

# Guardrail: refuse to ship a distribution that names license/background (or any
# other resource file) we did not stage. Catches regressions without needing a
# macOS runner in unit tests.
python3 - "${DIST_DIR}/distribution.xml" "${RESOURCES_STAGING}" <<'PY'
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

dist_path = Path(sys.argv[1])
resources = Path(sys.argv[2])
root = ET.parse(dist_path).getroot()
# ElementTree expands the default ns oddly for this doctype-free XML; tags are local.
forbidden = {"license", "background", "background-darkAqua"}
errors = []
for elem in root.iter():
    tag = elem.tag.split("}")[-1]
    path = elem.get("file")
    if tag in forbidden:
        errors.append(f"<{tag}> must not appear unless its resource is shipped (see #118)")
    if path is None:
        continue
    if not (resources / path).is_file():
        errors.append(f"<{tag} file={path!r}> missing under {resources}")
if errors:
    print("ERROR: distribution.xml resource check failed:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
print(f"distribution.xml OK — {len(list(resources.iterdir()))} staged resource(s)")
PY

PRODUCTBUILD_SIGN=()
if [ -n "${PKG_SIGN_ID}" ]; then
    PRODUCTBUILD_SIGN=(--sign "${PKG_SIGN_ID}" --timestamp)
fi
productbuild \
    --distribution "${DIST_DIR}/distribution.xml" \
    --package-path "${DIST_DIR}" \
    --resources "${RESOURCES_STAGING}" \
    ${PRODUCTBUILD_SIGN[@]+"${PRODUCTBUILD_SIGN[@]}"} \
    "${FINAL_PKG}"

# ── Notarize + staple ──────────────────────────────────────────────────────────
NOTARY_ARGS=()
if [ -n "${NOTARY_PROFILE:-}" ]; then
    NOTARY_ARGS=(--keychain-profile "${NOTARY_PROFILE}")
elif [ -n "${APPLE_API_KEY_PATH:-}" ] && [ -n "${APPLE_API_KEY_ID:-}" ] && [ -n "${APPLE_API_ISSUER:-}" ]; then
    NOTARY_ARGS=(--key "${APPLE_API_KEY_PATH}" --key-id "${APPLE_API_KEY_ID}" --issuer "${APPLE_API_ISSUER}")
fi

if [ ${#NOTARY_ARGS[@]} -gt 0 ]; then
    if [ -z "${PKG_SIGN_ID}" ] || [ -z "${SIGN_ID}" ]; then
        echo "ERROR: notarization needs both APP_SIGN_ID and PKG_SIGN_ID."
        exit 1
    fi
    echo "Submitting ${FINAL_PKG} for notarization..."
    # Submit without --wait and poll instead. A first submission from a new team can sit "In Progress" for an hour
    # or more, and a single dropped connection during a long --wait used to be reported as a rejection.
    SUB_ID=""
    for attempt in 1 2 3 4 5; do
        SUBMIT_OUT="$(xcrun notarytool submit "${FINAL_PKG}" "${NOTARY_ARGS[@]}" --output-format json 2>&1)" || true
        SUB_ID="$(echo "${SUBMIT_OUT}" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: pass')"
        [ -n "${SUB_ID}" ] && break
        echo "Submit attempt ${attempt} failed: ${SUBMIT_OUT}"
        sleep 30
    done
    if [ -z "${SUB_ID}" ]; then
        echo "ERROR: could not submit ${FINAL_PKG} for notarization."
        exit 1
    fi
    echo "Notarization submission id: ${SUB_ID}"

    NOTARY_TIMEOUT="${NOTARY_TIMEOUT_MINUTES:-180}"
    DEADLINE=$(( $(date +%s) + NOTARY_TIMEOUT * 60 ))
    STATUS=""
    while :; do
        INFO="$(xcrun notarytool info "${SUB_ID}" "${NOTARY_ARGS[@]}" --output-format json 2>&1)" || INFO=""
        # A failed poll (network blip, Apple 5xx) is not a verdict: keep waiting.
        STATUS="$(echo "${INFO}" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("status",""))
except Exception: pass')"
        case "${STATUS}" in
            Accepted) echo "Notarization accepted."; break ;;
            Invalid|Rejected)
                echo "ERROR: Apple rejected the submission (${STATUS}). Their log:"
                xcrun notarytool log "${SUB_ID}" "${NOTARY_ARGS[@]}" || true
                exit 1 ;;
        esac
        if [ "$(date +%s)" -ge "${DEADLINE}" ]; then
            echo "ERROR: notarization still '${STATUS:-unknown}' after ${NOTARY_TIMEOUT} min. Submission ${SUB_ID} is still with Apple;"
            echo "check it with: xcrun notarytool info ${SUB_ID} <credentials>"
            exit 1
        fi
        echo "$(date -u +%H:%M:%S) notarization status: ${STATUS:-unreachable, retrying}"
        sleep 60
    done
    xcrun stapler staple "${FINAL_PKG}"
    xcrun stapler validate "${FINAL_PKG}"
    spctl --assess --type install --verbose=2 "${FINAL_PKG}"
else
    echo "Skipping notarization (no NOTARY_PROFILE / APPLE_API_* credentials set)"
fi

# Clean up staging artifacts
rm -rf "${PKG_STAGING}" "${SCRIPTS_STAGING}" "${RESOURCES_STAGING}" \
    "${COMPONENT_PKG}" "${DIST_DIR}/distribution.xml"

echo ""
echo "=== Build complete ==="
echo "  Installer:  ${FINAL_PKG}"
echo "  Talk to it:  http://localhost:5173/chat (opens after install)"
echo "  Background service: ${APP_NAME}.app"
echo ""