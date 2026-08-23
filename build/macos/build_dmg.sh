#!/bin/bash
# The Telescope Net Node Agent — macOS pkg / dmg builder
#
# Usage:  bash build/macos/build_dmg.sh [--sign "Developer ID: ..."]
#
# Prerequisites:
#   PyInstaller bundle already built at dist/TelescopeNetNode
#   Flutter desktop bundle built at app/build/macos/Build/Products/Release/boundless_skies.app
#   pkgbuild + productbuild  (Xcode command-line tools)
#
# Outputs:
#   dist/TelescopeNetNode-X.Y.Z-macOS.pkg   (GUI installer)

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

SIGN_ID=""
if [ "$1" = "--sign" ]; then
    SIGN_ID="$2"
fi

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
if [ -n "${SIGN_ID}" ]; then
    echo "Code-signing with: ${SIGN_ID}"
    codesign --deep --force --options runtime \
        --sign "${SIGN_ID}" \
        "${BUNDLE_DIR}"
    codesign --verify --deep --strict "${BUNDLE_DIR}"
else
    echo "Skipping code signing (pass --sign 'Developer ID: ...' to sign)"
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
RESOURCES_SRC="${BUILD_DIR}/resources"
mkdir -p "${RESOURCES_SRC}"

# Write welcome screen if absent
if [ ! -f "${RESOURCES_SRC}/welcome.html" ]; then
    cat > "${RESOURCES_SRC}/welcome.html" <<'HTML'
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/></head>
<body style="font-family: -apple-system, Helvetica; font-size: 13px; color: #1a1a1a; padding: 20px;">
<h2 style="color:#1a1a1a;">Welcome to The Telescope Net</h2>
<p>This installer sets up the <strong>Telescope Net desktop app</strong> and its always-on local node service.</p>
<p>Your AI is your control surface. The node service runs silently in the background, connecting your telescope to the TTN network and contributing science-quality photometry to the global variable star record.</p>
<p><strong>Works with your telescope</strong></p>
<p>TTN supports a wide range of equipment, including:</p>
<ul>
<li><strong>Smart telescopes</strong> — ZWO Seestar S50 &amp; S30, Vaonis Vespera / Vespera II / Vespera Pro / Stellina / Hyperia, DwarfLab Dwarf II / Dwarf 3 / Dwarf S, Unistellar eVscope / eVscope 2 / eQuinox / eQuinox 2 / Odyssey, Celestron Origin</li>
<li><strong>Traditional &amp; GOTO setups</strong> — Celestron, Meade, Sky-Watcher, William Optics, Takahashi, and many more via ALPACA/ASCOM</li>
</ul>
<p>After installation, the setup wizard will walk you through selecting your telescope and downloading the star catalog needed for accurate plate solving (~6 GB, downloaded in the background).</p>
<p><strong>Requirements</strong></p>
<ul>
<li>macOS 11 or later</li>
<li>Your telescope connected to your local WiFi or via USB</li>
</ul>
<p>Everything else — Python, the plate solver, all science libraries — is bundled. No separate downloads required.</p>
<p style="color:#555;">After installation, The Telescope Net desktop app opens automatically. It connects to the background node service on this Mac.</p>
<p style="color:#555;">Sign in and tap "Connect telescope" to link this computer to your member account. There is no activation code to enter.</p>
</body>
</html>
HTML
fi

cat > "${DIST_DIR}/distribution.xml" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
    <title>The Telescope Net ${VERSION}</title>
    <welcome file="welcome.html" mime-type="text/html"/>
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

productbuild \
    --distribution "${DIST_DIR}/distribution.xml" \
    --package-path "${DIST_DIR}" \
    --resources "${RESOURCES_SRC}" \
    "${FINAL_PKG}"

# Clean up staging artifacts
rm -rf "${PKG_STAGING}" "${SCRIPTS_STAGING}" "${COMPONENT_PKG}" "${DIST_DIR}/distribution.xml"

echo ""
echo "=== Build complete ==="
echo "  Installer:  ${FINAL_PKG}"
echo "  Talk to it:  http://localhost:5173/chat (opens after install)"
echo "  Background service: ${APP_NAME}.app"
echo ""
