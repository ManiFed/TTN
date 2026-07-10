#!/bin/bash
# The Telescope Net Node Agent — macOS postinstall script
#
# Called by the macOS .pkg installer after the payload is copied.
# Runs as root.  (pkgbuild requires this file to be staged as "postinstall"
# with no extension — see build_dmg.sh.)
#
# This script:
#   1. Creates the data directory
#   2. Writes config.yaml from the template (substituting the activation code)
#   3. Installs the launchd plist and starts the service
#   4. Configures system power settings to prevent sleep
#   5. Opens the local dashboard (or Flutter desktop app) for the console user

set -e

APP_DIR="/Applications/TelescopeNetNode.app"
DESKTOP_APP="/Applications/TelescopeNet.app"
DATA_DIR="/Library/Application Support/TelescopeNet/NodeAgent"
LOG_DIR="/Library/Logs/TelescopeNet"
PLIST_SRC="${APP_DIR}/Contents/Resources/com.telescopenet.nodeagent.plist"
PLIST_DEST="/Library/LaunchDaemons/com.telescopenet.nodeagent.plist"
ACTIVATION_CODE="${BS_ACTIVATION_CODE:-}"    # Optional: supplied by scripted installs

echo "=== The Telescope Net Node Agent — postinstall ==="

# ── Create directories ─────────────────────────────────────────────────────────
install -d -m 755 "${DATA_DIR}"
install -d -m 755 "${DATA_DIR}/data"
install -d -m 755 "${DATA_DIR}/logs"
install -d -m 755 "${DATA_DIR}/fits_export"
install -d -m 755 "${DATA_DIR}/aavso_submissions"
install -d -m 755 "${LOG_DIR}"

# ── Write config.yaml ──────────────────────────────────────────────────────────
CONFIG="${DATA_DIR}/config.yaml"
# Template ships inside the .app bundle Resources (not app root).
TEMPLATE="${APP_DIR}/Contents/Resources/config.template.yaml"
if [ ! -f "${TEMPLATE}" ]; then
    # Older package layouts / fallbacks
    TEMPLATE="${APP_DIR}/config.template.yaml"
fi

if [ ! -f "${CONFIG}" ]; then
    if [ ! -f "${TEMPLATE}" ]; then
        echo "WARNING: config template missing — writing minimal config"
        cat > "${CONFIG}" <<'YAML'
cloud:
  enabled: true
  url: 'https://api.thetelescope.net'
  activation_code: ''
YAML
    else
        cp "${TEMPLATE}" "${CONFIG}"
        if [ -n "${ACTIVATION_CODE}" ]; then
            sed -i '' "s/ACTIVATION_CODE_PLACEHOLDER/${ACTIVATION_CODE}/g" "${CONFIG}"
            echo "Activation code written to config.yaml"
        else
            sed -i '' "s/ACTIVATION_CODE_PLACEHOLDER//g" "${CONFIG}"
            echo "No activation code supplied — dashboard setup will ask for one"
        fi
    fi
    chmod 600 "${CONFIG}"
fi

# ── Prevent idle sleep ─────────────────────────────────────────────────────────
# Disable idle sleep on AC power (does not affect battery sleep)
pmset -c sleep 0 2>/dev/null || true
pmset -c disksleep 0 2>/dev/null || true
echo "Power management configured: AC idle sleep disabled"

# ── Install and start the launchd service ─────────────────────────────────────
# Unload any existing version first
if [ -f "${PLIST_DEST}" ]; then
    launchctl bootout system "${PLIST_DEST}" 2>/dev/null || true
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
fi
# Also stop any user-launched copy so launchd can bind :5173
killall TelescopeNetNode 2>/dev/null || true

if [ ! -f "${PLIST_SRC}" ]; then
    echo "ERROR: launchd plist missing at ${PLIST_SRC}"
    exit 1
fi

# Copy plist and fix ownership
cp "${PLIST_SRC}" "${PLIST_DEST}"
chown root:wheel "${PLIST_DEST}"
chmod 644 "${PLIST_DEST}"

# Load and start (prefer modern bootstrap; fall back to load)
if launchctl bootstrap system "${PLIST_DEST}" 2>/dev/null; then
    launchctl enable system/com.telescopenet.nodeagent 2>/dev/null || true
    launchctl kickstart -k system/com.telescopenet.nodeagent 2>/dev/null || true
else
    launchctl load -w "${PLIST_DEST}"
fi
echo "Service installed and started: com.telescopenet.nodeagent"

# ── Open the UI for the logged-in desktop user ────────────────────────────────
# Installer scripts run as root; open inside the console user's GUI session.
DASHBOARD_URL="http://localhost:5173"
CONSOLE_USER="$(stat -f %Su /dev/console 2>/dev/null || true)"
if [ -n "${CONSOLE_USER}" ] && [ "${CONSOLE_USER}" != "root" ] && [ "${CONSOLE_USER}" != "loginwindow" ]; then
    CONSOLE_UID="$(id -u "${CONSOLE_USER}" 2>/dev/null || true)"
    if [ -n "${CONSOLE_UID}" ]; then
        # Give launchd a few seconds to bind the dashboard port before opening.
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if /usr/bin/curl -fsS "${DASHBOARD_URL}/api/status" >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done
        if [ -d "${DESKTOP_APP}" ]; then
            launchctl asuser "${CONSOLE_UID}" /usr/bin/open -a "${DESKTOP_APP}" || true
            echo "Desktop app opened for ${CONSOLE_USER}: ${DESKTOP_APP}"
        else
            launchctl asuser "${CONSOLE_UID}" /usr/bin/open "${DASHBOARD_URL}" || true
            echo "Dashboard opened for ${CONSOLE_USER}: ${DASHBOARD_URL}"
        fi
    fi
fi

echo ""
echo "Installation complete!"
echo "Desktop app: ${DESKTOP_APP} (if packaged)"
echo "Dashboard:   ${DASHBOARD_URL}"
echo "Service:     com.telescopenet.nodeagent"
echo "Logs:        ${LOG_DIR}/node_agent.log"
echo ""
echo "To stop / uninstall later:"
echo "  sudo launchctl bootout system ${PLIST_DEST}"
echo "  sudo rm -f ${PLIST_DEST}"
echo "  sudo rm -rf ${APP_DIR} ${DESKTOP_APP}"
echo ""
