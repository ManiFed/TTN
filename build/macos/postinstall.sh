#!/bin/bash
# The Telescope Net Node Agent — macOS postinstall script
#
# Called by the macOS .pkg installer after the payload is copied.
# Runs as root.  (pkgbuild requires this file to be staged as "postinstall"
# with no extension — see build_dmg.sh.)
#
# This script:
#   1. Determines the console (logged-in) user -- the agent runs as THEM,
#      not root: a root LaunchDaemon has no GUI session and so can never
#      reach the login keychain, which silently broke API key persistence
#      across every restart (see com.boundlessskies.nodeagent.plist).
#   2. Creates that user's data directory
#   3. Writes config.yaml from the template
#   4. Migrates an old root-daemon install's state, if one exists
#   5. Installs the LaunchAgent (in the user's gui/<uid> domain) and starts it
#   6. Configures system power settings to prevent sleep
#   7. Opens the Flutter desktop app for the console user

set -e

APP_DIR="/Applications/TelescopeNetNode.app"
DESKTOP_APP="/Applications/TelescopeNet.app"
echo "=== The Telescope Net Node Agent — postinstall ==="

# ── Identify the console user -- the agent runs as them, not root ─────────────
CONSOLE_USER="$(stat -f %Su /dev/console 2>/dev/null || true)"
if [ -z "${CONSOLE_USER}" ] || [ "${CONSOLE_USER}" = "root" ] || [ "${CONSOLE_USER}" = "loginwindow" ]; then
    echo "ERROR: no console user logged in -- cannot install a per-user LaunchAgent."
    echo "Log in to the Mac's desktop session and re-run this installer."
    exit 1
fi
CONSOLE_UID="$(id -u "${CONSOLE_USER}")"
USER_HOME="$(dscl . -read /Users/"${CONSOLE_USER}" NFSHomeDirectory | awk '{print $2}')"
echo "Installing for console user: ${CONSOLE_USER} (uid ${CONSOLE_UID}, home ${USER_HOME})"

DATA_DIR="${USER_HOME}/Library/Application Support/TelescopeNet/NodeAgent"
LOG_DIR="${USER_HOME}/Library/Logs/TelescopeNet"
PLIST_SRC="${APP_DIR}/Contents/Resources/com.telescopenet.nodeagent.plist"
PLIST_DEST="${USER_HOME}/Library/LaunchAgents/com.telescopenet.nodeagent.plist"

# ── Retire an old root-LaunchDaemon install, migrating its state ──────────────
OLD_PLIST="/Library/LaunchDaemons/com.telescopenet.nodeagent.plist"
OLD_DATA_DIR="/Library/Application Support/TelescopeNet/NodeAgent"
if [ -f "${OLD_PLIST}" ]; then
    echo "Retiring previous system-daemon install..."
    launchctl bootout system "${OLD_PLIST}" 2>/dev/null || true
    launchctl unload "${OLD_PLIST}" 2>/dev/null || true
    rm -f "${OLD_PLIST}"
    # Carry over node_id/pair_token/config so this doesn't register as a new
    # node yet again -- the API key itself never persisted under the old
    # daemon (that was the bug), so one more re-registration is unavoidable,
    # but it'll be the last one: the new LaunchAgent can actually save it.
    if [ -d "${OLD_DATA_DIR}" ] && [ ! -d "${DATA_DIR}" ]; then
        mkdir -p "$(dirname "${DATA_DIR}")"
        cp -R "${OLD_DATA_DIR}" "${DATA_DIR}"
        # cp -R preserves the old root ownership; the agent now runs as the
        # console user and needs to write these files (e.g. cloud_state.json).
        chown -R "${CONSOLE_USER}:staff" "${DATA_DIR}"
        echo "Migrated previous config/state from ${OLD_DATA_DIR}"
    fi
fi

# ── Create directories, owned by the console user ─────────────────────────────
install -d -o "${CONSOLE_USER}" -g staff -m 755 "${DATA_DIR}"
install -d -o "${CONSOLE_USER}" -g staff -m 755 "${DATA_DIR}/data"
install -d -o "${CONSOLE_USER}" -g staff -m 755 "${DATA_DIR}/logs"
install -d -o "${CONSOLE_USER}" -g staff -m 755 "${DATA_DIR}/fits_export"
install -d -o "${CONSOLE_USER}" -g staff -m 755 "${DATA_DIR}/aavso_submissions"
install -d -o "${CONSOLE_USER}" -g staff -m 755 "${LOG_DIR}"
install -d -o "${CONSOLE_USER}" -g staff -m 755 "${USER_HOME}/Library/LaunchAgents"

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
YAML
    else
        cp "${TEMPLATE}" "${CONFIG}"
        # Strip any leftover legacy activation-code placeholders.
        sed -i '' "s/ACTIVATION_CODE_PLACEHOLDER//g" "${CONFIG}" 2>/dev/null || true
        echo "Config seeded — link from The Telescope Net app → Connect telescope"
    fi
fi
chown "${CONSOLE_USER}:staff" "${CONFIG}"
chmod 600 "${CONFIG}"

# ── Register the MCP server with Claude Desktop ───────────────────────────────
# So members never hand-edit claude_desktop_config.json. That step is invisible
# in an installer, easy to get subtly wrong, and fails silently -- the tools
# simply never appear, with nothing to search for.
#
# The script merges one key and refuses to touch a config it cannot parse, so a
# member's other MCP servers are never disturbed. Run as the console user: the
# config lives in their home, and a root-owned file there would break Claude.
# Never fatal -- Claude Desktop not being installed is a normal outcome.
AGENT_BIN="${APP_DIR}/Contents/MacOS/TelescopeNetNode"
if [ -x "${AGENT_BIN}" ]; then
    if sudo -u "${CONSOLE_USER}" "${AGENT_BIN}" --register-mcp \
            --data-dir "${DATA_DIR}"; then
# Whether this member already has an assistant that can drive the telescope.
# Decided once so the open, the closing message and the registration note
# cannot disagree with each other.
HAS_ASSISTANT=0
# Written as an `if` for legibility. (`[ -d x ] && VAR=1` also works here --
# `set -e` does not fire on a failing AND-list in a loop body -- but a reader
# has to know that to be sure, and this script is one people edit rarely.)
for candidate in "/Applications/Claude.app" "/Applications/Cursor.app" \
                 "/Applications/Windsurf.app"; do
    if [ -d "${candidate}" ]; then
        HAS_ASSISTANT=1
    fi
done

        MCP_REGISTERED=1
    else
        echo "NOTE: could not register the MCP server automatically."
        echo "      The telescope still works; AI control needs manual setup."
    fi
else
    echo "NOTE: node agent binary missing; skipping MCP registration."
fi

# ── Prevent idle sleep ─────────────────────────────────────────────────────────
# Disable idle sleep on AC power (does not affect battery sleep)
pmset -c sleep 0 2>/dev/null || true
pmset -c disksleep 0 2>/dev/null || true
echo "Power management configured: AC idle sleep disabled"

# ── Install and start the LaunchAgent, in the user's GUI session ──────────────
# Unload any existing copy first
if [ -f "${PLIST_DEST}" ]; then
    launchctl bootout "gui/${CONSOLE_UID}" "${PLIST_DEST}" 2>/dev/null || true
    sudo -u "${CONSOLE_USER}" launchctl unload "${PLIST_DEST}" 2>/dev/null || true
fi
# Also stop any already-running copy so the new one can bind :5173
sudo -u "${CONSOLE_USER}" killall TelescopeNetNode 2>/dev/null || true

if [ ! -f "${PLIST_SRC}" ]; then
    echo "ERROR: launchd plist missing at ${PLIST_SRC}"
    exit 1
fi

# Substitute the real home directory into the plist template, then install
# it owned by the console user (launchd requires this for a user LaunchAgent).
HOME_ESCAPED=$(printf '%s' "${USER_HOME}" | sed 's/[&/\]/\\&/g')
sed "s|__HOME__|${HOME_ESCAPED}|g" "${PLIST_SRC}" > "${PLIST_DEST}"
chown "${CONSOLE_USER}:staff" "${PLIST_DEST}"
chmod 644 "${PLIST_DEST}"

# Load and start as the console user (prefer modern bootstrap; fall back to load)
if launchctl asuser "${CONSOLE_UID}" launchctl bootstrap "gui/${CONSOLE_UID}" "${PLIST_DEST}" 2>/dev/null; then
    launchctl asuser "${CONSOLE_UID}" launchctl enable "gui/${CONSOLE_UID}/com.telescopenet.nodeagent" 2>/dev/null || true
    launchctl asuser "${CONSOLE_UID}" launchctl kickstart -k "gui/${CONSOLE_UID}/com.telescopenet.nodeagent" 2>/dev/null || true
else
    sudo -u "${CONSOLE_USER}" launchctl load -w "${PLIST_DEST}"
fi
echo "Service installed and started: com.telescopenet.nodeagent (as ${CONSOLE_USER})"

# ── Open the UI for the logged-in desktop user ────────────────────────────────
DASHBOARD_URL="http://localhost:5173"
# Wait for the agent to bind its port, and remember whether it ever did.
#
# The old loop gave up after ten seconds and carried on regardless, so a first
# run that took longer ended with a browser pointed at a refused connection.
# Ten seconds is not enough on a first run: a freshly installed, unsigned
# PyInstaller bundle pays Gatekeeper verification and a one-off unpack before
# any of our code executes.
AGENT_READY=0
for _ in $(seq 1 60); do
    if /usr/bin/curl -fsS "${DASHBOARD_URL}/api/status" >/dev/null 2>&1; then
        AGENT_READY=1
        break
    fi
    sleep 1
done
if [ "${AGENT_READY}" -eq 0 ]; then
    echo "NOTE: the node software has not answered yet. It may still be"
    echo "      starting; give it a minute, then open ${DASHBOARD_URL}/chat"
fi

# ── Unwrap a Gatekeeper ".localized" quarantine rename ─────────────────────────
# Kept for upgrades from installs that shipped the desktop app: the rename
# leaves a stray folder in /Applications that confuses Finder whether or not
# anything opens the bundle.
# Because this .pkg (and the app inside it) aren't signed/notarized, macOS
# Installer sometimes can't validate the payload during copy and defensively
# renames the destination folder by appending ".localized", leaving the real
# app nested one level inside it instead of at DESKTOP_APP. Self-heal that
# here so members never have to do this by hand.
LOCALIZED_WRAPPER="${DESKTOP_APP%.app}.localized"
if [ ! -d "${DESKTOP_APP}" ] && [ -d "${LOCALIZED_WRAPPER}/$(basename "${DESKTOP_APP}")" ]; then
    echo "Found Gatekeeper-renamed app at ${LOCALIZED_WRAPPER}; unwrapping..."
    mv "${LOCALIZED_WRAPPER}/$(basename "${DESKTOP_APP}")" "${DESKTOP_APP}"
    rm -rf "${LOCALIZED_WRAPPER}"
fi

# Open the chat page only when it is the interface this member will use.
#
# Someone who already has an assistant installed has just had their telescope
# registered with it; opening a second, unfamiliar chat window on top of that
# is confusing rather than helpful. They get told to restart the assistant
# instead, which is the step they actually need.
#
# And never open a page that is not being served: a refused connection reads
# as "the install failed", when the agent is usually just still starting.
if [ "${AGENT_READY}" -eq 1 ] && [ "${HAS_ASSISTANT}" -eq 0 ]; then
    launchctl asuser "${CONSOLE_UID}" /usr/bin/open "${DASHBOARD_URL}/chat" || true
    echo "Opened ${DASHBOARD_URL}/chat for ${CONSOLE_USER}"
fi

echo ""
echo "Installation complete!"
echo ""
# Nothing else in the product mentions this, so if it is not said here a member
# has no way of discovering that their telescope can be driven by asking.
if [ "${MCP_REGISTERED:-0}" = "1" ]; then
    # ChatGPT is deliberately not counted: it reaches MCP servers only as
    # remote connectors, so it cannot start this one or see the telescope.
    if [ "${HAS_ASSISTANT}" -eq 1 ]; then
        echo "You can now run this telescope by asking."
        echo "  Quit and reopen your AI assistant, then say:"
        echo "      connect my telescope"
        echo "  (It needs a restart to notice the new tools.)"
    else
        echo "This telescope can be run by asking, if you use an AI assistant."
        echo "  Install Claude Desktop (https://claude.ai/download), Cursor or"
        echo "  Windsurf — the telescope is already set up in it — then say:"
        echo "      connect my telescope"
        echo ""
        echo "  ChatGPT will not work for this: it can only reach assistants'"
        echo "  tools over the internet, and your telescope is on your own"
        echo "  network where it cannot see it."
    fi
    echo ""
fi
echo "Talk to it:  ${DASHBOARD_URL}/chat"
echo "Dashboard:   ${DASHBOARD_URL}"
echo "Service:     com.telescopenet.nodeagent (gui/${CONSOLE_UID})"
echo "Logs:        ${LOG_DIR}/node_agent.log"
echo ""
echo "To stop / uninstall later:"
echo "  launchctl bootout gui/${CONSOLE_UID} ${PLIST_DEST}"
echo "  rm -f ${PLIST_DEST}"
echo "  sudo rm -rf ${APP_DIR} ${DESKTOP_APP}"
echo ""
