#!/bin/bash
# Warns when the installed macOS app predates the source tree it came from.
#
# Three reported failures in one evening turned out to be the same thing: the
# fix landed in source, and the running app was built hours earlier. Every
# diagnosis session re-checked the code and never checked whether the code was
# actually running, because nothing said so.
#
# Usage:
#   scripts/check_installed_build.sh
set -euo pipefail

APP="/Applications/TelescopeNetNode.app/Contents/MacOS/TelescopeNetNode"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -f "${APP}" ]; then
    echo "Not installed: ${APP}"
    exit 0
fi

BUILT_EPOCH=$(stat -f "%m" "${APP}")
BUILT_HUMAN=$(stat -f "%Sm" "${APP}")
LATEST_COMMIT_EPOCH=$(cd "${REPO}" && git log -1 --format=%ct -- \
    src/ telescope_mcp/ build/ 2>/dev/null || echo 0)
LATEST_COMMIT_HUMAN=$(cd "${REPO}" && git log -1 --format=%ci -- \
    src/ telescope_mcp/ build/ 2>/dev/null || echo "unknown")

echo "Installed app built:  ${BUILT_HUMAN}"
echo "Latest source commit: ${LATEST_COMMIT_HUMAN}"

if [ "${LATEST_COMMIT_EPOCH}" -gt "${BUILT_EPOCH}" ]; then
    echo ""
    echo "STALE: the installed app predates a source change."
    echo "Rebuild with: python build/build.py --bundle-only --clean"
    exit 1
fi

echo "Up to date."
