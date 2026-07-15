#!/bin/bash
# Uninstall The Telescope Net Node Agent from macOS.
# Usage: sudo bash build/macos/uninstall.sh

set -euo pipefail

APP="/Applications/TelescopeNetNode.app"
DESKTOP="/Applications/TelescopeNet.app"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo:  sudo bash $0"
    exit 1
fi

CONSOLE_USER="$(stat -f %Su /dev/console 2>/dev/null || true)"

echo "Stopping service and processes..."

# Current installs: a per-user LaunchAgent (see postinstall.sh for why --
# a root LaunchDaemon can't reach the login keychain, so credentials never
# survived a restart).
if [ -n "${CONSOLE_USER}" ] && [ "${CONSOLE_USER}" != "root" ] && [ "${CONSOLE_USER}" != "loginwindow" ]; then
    CONSOLE_UID="$(id -u "${CONSOLE_USER}" 2>/dev/null || true)"
    USER_HOME="$(dscl . -read /Users/"${CONSOLE_USER}" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
    AGENT_PLIST="${USER_HOME}/Library/LaunchAgents/com.telescopenet.nodeagent.plist"
    if [ -n "${CONSOLE_UID}" ] && [ -f "${AGENT_PLIST}" ]; then
        launchctl asuser "${CONSOLE_UID}" launchctl bootout "gui/${CONSOLE_UID}" "${AGENT_PLIST}" 2>/dev/null || true
        rm -f "${AGENT_PLIST}"
    fi
fi

# Older installs: a root LaunchDaemon -- clean those up too if present.
DAEMON_PLIST="/Library/LaunchDaemons/com.telescopenet.nodeagent.plist"
if [ -f "${DAEMON_PLIST}" ]; then
    launchctl bootout system "${DAEMON_PLIST}" 2>/dev/null || true
    launchctl unload "${DAEMON_PLIST}" 2>/dev/null || true
    rm -f "${DAEMON_PLIST}"
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
echo "  rm -rf \"\$HOME/Library/Application Support/TelescopeNet\""
echo "  rm -rf \"\$HOME/Library/Logs/TelescopeNet\""
echo "  sudo rm -rf \"/Library/Application Support/TelescopeNet\" \"/Library/Logs/TelescopeNet\"  # pre-2026-07-15 installs"
echo ""
