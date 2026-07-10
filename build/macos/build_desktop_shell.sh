#!/bin/bash
# Build a native TelescopeNet.app shell (WKWebView → local node dashboard).
# Requires: Xcode Command Line Tools (swiftc). Does NOT require full Xcode/Flutter.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="${ROOT}/build/macos/desktop_shell/main.swift"
OUT_APP="${1:-${ROOT}/dist/TelescopeNet.app}"
VERSION="${2:-1.0.3}"
BUNDLE_ID="org.telescopenet.app"

if [ ! -f "${SRC}" ]; then
    echo "ERROR: missing ${SRC}"
    exit 1
fi
if ! command -v swiftc >/dev/null; then
    echo "ERROR: swiftc not found (install Xcode Command Line Tools)"
    exit 1
fi

echo "=== Building TelescopeNet.app shell v${VERSION} ==="
rm -rf "${OUT_APP}"
mkdir -p "${OUT_APP}/Contents/MacOS" "${OUT_APP}/Contents/Resources"

swiftc -O \
    -framework Cocoa \
    -framework WebKit \
    -target arm64-apple-macos11.0 \
    -o "${OUT_APP}/Contents/MacOS/TelescopeNet" \
    "${SRC}"
chmod +x "${OUT_APP}/Contents/MacOS/TelescopeNet"

cat > "${OUT_APP}/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleName</key>
    <string>The Telescope Net</string>
    <key>CFBundleDisplayName</key>
    <string>The Telescope Net</string>
    <key>CFBundleExecutable</key>
    <string>TelescopeNet</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
    <key>CFBundleGetInfoString</key>
    <string>The Telescope Net — local telescope control</string>
</dict>
</plist>
EOF

# PkgInfo is optional but traditional
echo -n 'APPL????' > "${OUT_APP}/Contents/PkgInfo"

echo "  Built: ${OUT_APP}"
ls -lh "${OUT_APP}/Contents/MacOS/TelescopeNet"
