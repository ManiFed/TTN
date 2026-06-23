#!/bin/bash
# The Telescope Net Node Agent — macOS pkg / dmg builder
#
# Usage:  bash build/macos/build_dmg.sh [--sign "Developer ID: ..."]
#
# Prerequisites:
#   PyInstaller bundle already built at dist/TelescopeNetNode
#   pkgbuild + productbuild  (Xcode command-line tools)
#   create-dmg (optional, brew install create-dmg)
#
# Outputs:
#   dist/TelescopeNetNode-X.Y.Z-macOS.pkg   (primary — GUI installer)
#   dist/TelescopeNetNode-X.Y.Z-macOS.dmg   (optional, if create-dmg present)

set -e
cd "$(dirname "$0")/../.."   # repo root

VERSION="1.0.0"
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

# ── Assemble .app bundle ───────────────────────────────────────────────────────
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
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
EOF

cp "${BUILD_DIR}/com.telescopenet.nodeagent.plist" "${RESOURCES_DIR}/"
cp "build/config.template.yaml" "${RESOURCES_DIR}/"
[ -f "build/icon.icns" ] && cp "build/icon.icns" "${RESOURCES_DIR}/AppIcon.icns"

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

pkgbuild \
    --root "${PKG_STAGING}" \
    --identifier "org.telescopenet.nodeagent" \
    --version "${VERSION}" \
    --scripts "${BUILD_DIR}" \
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
<h2 style="color:#1a1a1a;">Welcome to The Telescope Net Node Agent</h2>
<p>This installer will set up the <strong>The Telescope Net Node Agent</strong> on your Mac.</p>
<p>The Node Agent runs in the background as a system service, automatically:</p>
<ul>
<li>Connecting to your Seestar telescope</li>
<li>Downloading tonight's observation plan</li>
<li>Processing photometry and uploading science data</li>
</ul>
<p><strong>Requirements:</strong></p>
<ul>
<li>macOS 11 or later</li>
<li>Seestar S50 or S30 in Station Mode on your local WiFi</li>
<li>A The Telescope Net activation code (get one at telescopenet.org)</li>
<li>ASTAP plate solver (<a href="https://www.hnsky.org/astap.htm">hnsky.org/astap.htm</a>)</li>
</ul>
<p style="color:#555;">After installation, add your activation code to:<br/>
<code>/Library/Application Support/TelescopeNet/NodeAgent/config.yaml</code></p>
</body>
</html>
HTML
fi

cat > "${DIST_DIR}/distribution.xml" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
    <title>The Telescope Net Node Agent ${VERSION}</title>
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
rm -rf "${PKG_STAGING}" "${COMPONENT_PKG}" "${DIST_DIR}/distribution.xml"

# ── Optional DMG (drag-to-Applications) ───────────────────────────────────────
if command -v create-dmg &>/dev/null; then
    echo "Building .dmg..."
    create-dmg \
        --volname "The Telescope Net Node Agent" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "${APP_NAME}.app" 150 190 \
        --hide-extension "${APP_NAME}.app" \
        --app-drop-link 450 190 \
        "${DIST_DIR}/${APP_NAME}-${VERSION}-macOS.dmg" \
        "${BUNDLE_DIR}"
else
    echo "(create-dmg not found — skipping .dmg, install with: brew install create-dmg)"
fi

echo ""
echo "=== Build complete ==="
echo "  Installer:  ${FINAL_PKG}"
[ -f "${DIST_DIR}/${APP_NAME}-${VERSION}-macOS.dmg" ] && \
echo "  DMG:        ${DIST_DIR}/${APP_NAME}-${VERSION}-macOS.dmg"
echo ""
