#!/bin/bash
# The Telescope Net Node Agent — Linux installer
#
# Usage:
#   curl -sSL https://telescopenet.org/install.sh | sudo bash
#   -- or --
#   sudo bash build/linux/install.sh [--code BS-YYYY-XXXXXXXX]
#
# Supports: Debian/Ubuntu, Fedora/RHEL, Arch Linux
# Requires: systemd, curl or wget, internet access

set -e

INSTALL_DIR="/opt/telescopenet/nodeagent"
DATA_DIR="/var/lib/telescopenet/nodeagent"
LOG_DIR="/var/log/telescopenet"
SERVICE_USER="telescopenet"
SERVICE_FILE="/etc/systemd/system/telescopenet-node.service"
RELEASE_URL="https://api.thetelescope.net/download/node-agent/linux"
ACTIVATION_CODE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --code) ACTIVATION_CODE="$2"; shift 2 ;;
        --url)  RELEASE_URL="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Must run as root
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This installer must be run as root (use sudo)"
    exit 1
fi

# Check for systemd
if ! command -v systemctl &>/dev/null; then
    echo "ERROR: systemd is required but not found"
    exit 1
fi

echo ""
echo "=== The Telescope Net Node Agent — Linux Installer ==="
echo ""

# ── Create service user ────────────────────────────────────────────────────────
if ! id "${SERVICE_USER}" &>/dev/null; then
    echo "Creating service user: ${SERVICE_USER}"
    useradd --system --home-dir "${DATA_DIR}" --shell /bin/false \
        --comment "The Telescope Net Node Agent" "${SERVICE_USER}"
fi

# ── Create directories ─────────────────────────────────────────────────────────
echo "Creating directories..."
install -d -m 755 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${INSTALL_DIR}"
install -d -m 755 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_DIR}"
install -d -m 755 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_DIR}/data"
install -d -m 755 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_DIR}/logs"
install -d -m 755 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_DIR}/fits_export"
install -d -m 755 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DATA_DIR}/aavso_submissions"
install -d -m 755 "${LOG_DIR}"

# ── Download binary ────────────────────────────────────────────────────────────
echo "Downloading Node Agent binary..."
if command -v curl &>/dev/null; then
    curl -fL "${RELEASE_URL}" -o "${INSTALL_DIR}/TelescopeNetNode"
elif command -v wget &>/dev/null; then
    wget -q "${RELEASE_URL}" -O "${INSTALL_DIR}/TelescopeNetNode"
else
    echo "ERROR: curl or wget is required"
    exit 1
fi
chmod 755 "${INSTALL_DIR}/TelescopeNetNode"
chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/TelescopeNetNode"

# ── Write config.yaml ──────────────────────────────────────────────────────────
CONFIG="${DATA_DIR}/config.yaml"
if [ ! -f "${CONFIG}" ]; then
    echo "Writing config.yaml..."
    # The binary bundles config.template.yaml; extract it if possible
    if [ -f "/tmp/config.template.yaml" ]; then
        cp "/tmp/config.template.yaml" "${CONFIG}"
    else
        # Minimal inline config if template not available
        cat > "${CONFIG}" <<'YAML'
cloud:
  enabled: true
  url: 'https://api.thetelescope.net'
  activation_code: 'ACTIVATION_CODE_PLACEHOLDER'
  node_id: ''
  api_key: ''
  heartbeat_interval: 60
  plan_poll_interval: 300
  auto_run_plans: true
  upload_images: false
image_watcher:
  enabled: true
  watch_path: '/mnt/seestar'
  debounce_delay: 2.0
photometry:
  enabled: true
  node_id: ''
  filter_name: CV
  astap_path: astap
logging:
  level: INFO
YAML
    fi

    if [ -n "${ACTIVATION_CODE}" ]; then
        sed -i "s/ACTIVATION_CODE_PLACEHOLDER/${ACTIVATION_CODE}/g" "${CONFIG}"
        echo "Activation code written to config.yaml"
    else
        sed -i "s/ACTIVATION_CODE_PLACEHOLDER//g" "${CONFIG}"
    fi

    chmod 600 "${CONFIG}"
    chown "${SERVICE_USER}:${SERVICE_USER}" "${CONFIG}"
fi

# ── Install systemd service ────────────────────────────────────────────────────
echo "Installing systemd service..."
# We either use the bundled service file or generate a minimal one
if [ -f "$(dirname "$0")/telescopenet-node.service" ]; then
    cp "$(dirname "$0")/telescopenet-node.service" "${SERVICE_FILE}"
else
    cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=The Telescope Net Node Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${DATA_DIR}
ExecStart=${INSTALL_DIR}/TelescopeNetNode --no-browser --data-dir ${DATA_DIR}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=telescopenet-node
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
fi

chmod 644 "${SERVICE_FILE}"

# ── Prevent idle sleep ─────────────────────────────────────────────────────────
echo "Configuring sleep prevention..."
# Disable suspend/hibernate targets in systemd (reversible with 'unmask')
systemctl mask sleep.target suspend.target hibernate.target \
    hybrid-sleep.target 2>/dev/null || true
echo "System sleep targets masked (telescope host won't sleep)"

# ── Enable and start the service ───────────────────────────────────────────────
echo "Enabling and starting the service..."
systemctl daemon-reload
systemctl enable telescopenet-node
systemctl start  telescopenet-node

# ── Add current user to service group (so they can read logs) ─────────────────
if [ -n "${SUDO_USER}" ]; then
    usermod -aG "${SERVICE_USER}" "${SUDO_USER}" 2>/dev/null || true
fi

# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo "=== Installation complete ==="
echo ""
echo "  Service status : systemctl status telescopenet-node"
echo "  Logs           : journalctl -u telescopenet-node -f"
echo "  Dashboard      : http://localhost:5173"
echo "  Config file    : ${CONFIG}"
echo ""

if [ -z "${ACTIVATION_CODE}" ]; then
    echo "NOTE: No activation code was provided."
    echo "Edit ${CONFIG} and add your code under cloud.activation_code,"
    echo "then restart: sudo systemctl restart telescopenet-node"
    echo ""
fi

systemctl status telescopenet-node --no-pager || true
