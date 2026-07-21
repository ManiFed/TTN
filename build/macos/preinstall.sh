#!/bin/bash
# The Telescope Net Node Agent — macOS preinstall script
#
# Called by the macOS .pkg installer BEFORE the payload is copied.
# Runs as root.  (pkgbuild requires this file to be staged as "preinstall"
# with no extension — see build_dmg.sh.)
#
# Why this exists: macOS's package installer compares each payload file
# against whatever's already on disk and can skip writing files it decides
# don't need updating. In practice this let /Applications/TelescopeNet.app
# survive completely untouched across multiple real-world reinstalls -- a
# member reinstalled three times in one night and kept running a build from
# days earlier, silently, with no error. Force-removing both app bundles
# here (before the payload step even starts) guarantees a clean overwrite
# every single install, regardless of what the installer's own diffing
# decides. Explicitly scoped to just these two paths -- never a general rm.

set -e

APP_DIR="/Applications/TelescopeNetNode.app"
DESKTOP_APP="/Applications/TelescopeNet.app"
echo "=== The Telescope Net — preinstall ==="

CONSOLE_USER="$(stat -f %Su /dev/console 2>/dev/null || true)"

# Stop anything currently running before removing its files, so the app
# isn't deleted out from under a live process mid-write.
if [ -n "${CONSOLE_USER}" ] && [ "${CONSOLE_USER}" != "root" ] && [ "${CONSOLE_USER}" != "loginwindow" ]; then
    sudo -u "${CONSOLE_USER}" killall TelescopeNetNode 2>/dev/null || true
    sudo -u "${CONSOLE_USER}" killall "The Telescope Net" 2>/dev/null || true
    sudo -u "${CONSOLE_USER}" killall TelescopeNet 2>/dev/null || true
fi

if [ -d "${APP_DIR}" ]; then
    echo "Removing existing ${APP_DIR} for a clean overwrite"
    rm -rf "${APP_DIR}"
fi
if [ -d "${DESKTOP_APP}" ]; then
    echo "Removing existing ${DESKTOP_APP} for a clean overwrite"
    rm -rf "${DESKTOP_APP}"
fi

exit 0
