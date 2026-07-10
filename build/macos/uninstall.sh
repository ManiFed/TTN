#!/bin/bash
# Uninstall The Telescope Net Node Agent from macOS.
# Usage: sudo bash build/macos/uninstall.sh

set -euo pipefail

PLIST="/Library/LaunchDaemons/com.telescopenet.nodeagent.plist"
APP="/Applications/TelescopeNetNode.app"
DESKTOP="/Applications/TelescopeNet.app"
SYS_DATA="/Library/Application Support/TelescopeNet"
SYS_LOGS="/Library/Logs/TelescopeNet"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo:  sudo bash $0"
    exit 1
fi

echo "Stopping service and processes..."
if [ -f "${PLIST}" ]; then
    launchctl bootout system "${PLIST}" 2>/dev/null || true
    launchctl unload "${PLIST}" 2>/dev/null || true
    rm -f "${PLIST}"
fi
killall TelescopeNetNode 2>/dev/null || true
# Give Launch Services / file locks a moment to release
sleep 1
killall -9 TelescopeNetNode 2>/dev/null || true

echo "Removing applications..."
rm -rf "${APP}" "${DESKTOP}"

echo "Removing package receipt..."
pkgutil --forget org.telescopenet.nodeagent 2>/dev/null || true

echo ""
echo "Removed service + apps."
echo "Optional (your config/logs) — delete manually if desired:"
echo "  sudo rm -rf \"${SYS_DATA}\" \"${SYS_LOGS}\""
echo "  rm -rf \"\$HOME/Library/Application Support/TelescopeNet\""
echo ""
