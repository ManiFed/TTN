#!/bin/bash
# The Telescope Net Node Agent — macOS postinstall script
#
# Called by the macOS .pkg installer after the payload is copied.
# Runs as root.
#
# This script:
#   1. Creates the data directory
#   2. Writes config.yaml from the template (substituting the activation code)
#   3. Installs the launchd plist and starts the service
#   4. Configures system power settings to prevent sleep

set -e

APP_DIR="/Applications/TelescopeNetNode.app"
DATA_DIR="/Library/Application Support/TelescopeNet/NodeAgent"
LOG_DIR="/Library/Logs/TelescopeNet"
PLIST_SRC="${APP_DIR}/Contents/Resources/com.telescopenet.nodeagent.plist"
PLIST_DEST="/Library/LaunchDaemons/com.telescopenet.nodeagent.plist"
ACTIVATION_CODE="${BS_ACTIVATION_CODE:-}"    # Set by the GUI installer page

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
TEMPLATE="${APP_DIR}/config.template.yaml"

if [ ! -f "${CONFIG}" ]; then
    cp "${TEMPLATE}" "${CONFIG}"
    if [ -n "${ACTIVATION_CODE}" ]; then
        sed -i '' "s/ACTIVATION_CODE_PLACEHOLDER/${ACTIVATION_CODE}/g" "${CONFIG}"
        echo "Activation code written to config.yaml"
    else
        sed -i '' "s/ACTIVATION_CODE_PLACEHOLDER//g" "${CONFIG}"
        echo "WARNING: No activation code provided — edit config.yaml to add it later"
    fi
    chmod 600 "${CONFIG}"
fi

# ── Prevent idle sleep ─────────────────────────────────────────────────────────
# Disable idle sleep on AC power (does not affect battery sleep)
pmset -c sleep 0
pmset -c disksleep 0
echo "Power management configured: AC idle sleep disabled"

# ── Install and start the launchd service ─────────────────────────────────────
# Unload any existing version first
if launchctl list | grep -q "com.telescopenet.nodeagent"; then
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
fi

# Copy plist and fix ownership
cp "${PLIST_SRC}" "${PLIST_DEST}"
chown root:wheel "${PLIST_DEST}"
chmod 644 "${PLIST_DEST}"

# Load and start
launchctl load -w "${PLIST_DEST}"
echo "Service installed and started: com.telescopenet.nodeagent"

echo ""
echo "Installation complete!"
echo "Dashboard: http://localhost:5173"
echo "Logs:      ${LOG_DIR}/node_agent.log"
echo ""
