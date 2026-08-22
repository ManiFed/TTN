#!/usr/bin/env python3
"""
The Telescope Net Node Agent — service entry point.

This is the entry point used by the packaged installers (Windows Service,
macOS LaunchDaemon, Linux systemd).  Unlike main.py (which has a dev-mode
file-watching watchdog), this runs dashboard.launch() directly in-process
with no subprocess spawning — compatible with PyInstaller bundles.

Usage:
    python main_service.py           # run on default port 5173
    python main_service.py --port N  # run on a different port
    python main_service.py --no-browser  # headless (service mode)
"""

import argparse
import logging
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

logger = logging.getLogger("main_service")

# Foreground desktop app installed alongside the headless node service.
_MACOS_DESKTOP_APP = pathlib.Path("/Applications/TelescopeNet.app")


def _default_data_dir() -> pathlib.Path:
    """Return a writable per-user data directory for a packaged app launch."""
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "TelescopeNet" / "NodeAgent"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return pathlib.Path(base) / "TelescopeNet" / "NodeAgent"
        return pathlib.Path.home() / "AppData" / "Local" / "TelescopeNet" / "NodeAgent"
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return pathlib.Path(base) / "telescopenet" / "nodeagent"
    return pathlib.Path.home() / ".local" / "share" / "telescopenet" / "nodeagent"


def _template_path() -> pathlib.Path | None:
    """Locate config.template.yaml in a frozen bundle or macOS app."""
    candidates = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(pathlib.Path(bundle_dir) / "config.template.yaml")
    executable = pathlib.Path(sys.executable).resolve()
    candidates.extend([
        executable.parent / "config.template.yaml",
        executable.parent.parent / "Resources" / "config.template.yaml",
    ])
    return next((path for path in candidates if path.is_file()), None)


def _prepare_data_dir(data_dir: pathlib.Path) -> None:
    """Create runtime directories and seed config on a first direct launch."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("data", "logs", "fits_export", "aavso_submissions"):
        (data_dir / name).mkdir(exist_ok=True)

    config_path = data_dir / "config.yaml"
    if config_path.exists():
        return
    template = _template_path()
    if template is not None:
        shutil.copyfile(template, config_path)
        raw = config_path.read_text(encoding="utf-8")
        config_path.write_text(
            raw.replace("ACTIVATION_CODE_PLACEHOLDER", ""),
            encoding="utf-8",
        )
    else:
        config_path.write_text(
            "cloud:\n"
            "  enabled: true\n"
            "  url: 'https://api.thetelescope.net'\n"
            "  node_id: ''\n"
            "  api_key: ''\n",
            encoding="utf-8",
        )
    try:
        config_path.chmod(0o600)
    except OSError:
        pass


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _open_desktop_ui() -> bool:
    """Open the native Telescope Net app window (never the browser)."""
    if sys.platform == "darwin":
        if _MACOS_DESKTOP_APP.is_dir():
            try:
                subprocess.Popen(
                    ["/usr/bin/open", "-a", str(_MACOS_DESKTOP_APP)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
                return True
            except OSError as exc:
                logger.error("Could not open desktop app: %s", exc)
                return False
        logger.error(
            "Desktop app missing at %s — install TelescopeNet.app",
            _MACOS_DESKTOP_APP,
        )
        return False
    # Non-macOS packaged launches: no browser fallback. The desktop app is
    # the only supported control surface.
    logger.warning("No desktop app open helper on this platform")
    return False


def _macos_gui_launch(port: int, data_dir: pathlib.Path) -> bool:
    """Handle double-click / Finder launch of the headless .app bundle.

    TelescopeNetNode.app is not a Cocoa UI — Launch Services would mark it
    "Application Not Responding" if it stayed in the foreground. Detach the
    background service if needed, open TelescopeNet.app, then exit.
    """
    if sys.platform != "darwin":
        return False
    if not getattr(sys, "frozen", False):
        return False
    if os.environ.get("TTN_NODE_DAEMON") == "1":
        return False

    if not _port_open(port):
        env = os.environ.copy()
        env["TTN_NODE_DAEMON"] = "1"
        cmd = [
            sys.executable,
            "--no-browser",
            "--port", str(port),
            "--data-dir", str(data_dir),
        ]
        try:
            subprocess.Popen(
                cmd,
                env=env,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            logger.error("Failed to start background node agent: %s", exc)
            return False

        for _ in range(60):
            if _port_open(port):
                break
            time.sleep(0.25)

    _open_desktop_ui()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="The Telescope Net Node Agent")
    parser.add_argument("--port", type=int, default=5173,
                        help="Dashboard port (default: 5173)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Start headless (service mode; do not open UI)")
    parser.add_argument("--data-dir", default="",
                        help="Working directory for config.yaml and data/ (default: current dir)")
    parser.add_argument("--mcp", action="store_true",
                        help="Serve the MCP tool interface on stdio instead of "
                             "running the dashboard (used by AI assistants)")
    args = parser.parse_args()

    if args.data_dir:
        data_dir = pathlib.Path(args.data_dir).expanduser()
    elif getattr(sys, "frozen", False):
        data_dir = _default_data_dir()
    else:
        data_dir = pathlib.Path.cwd()
    _prepare_data_dir(data_dir)
    os.chdir(data_dir)

    if args.mcp:
        _run_mcp_server()
        return

    _setup_service_logging()

    # Finder double-click of the headless service .app: detach + open real UI.
    if not args.no_browser and _macos_gui_launch(args.port, data_dir):
        return

    # Already running — open the desktop app if this was an interactive launch.
    if _port_open(args.port):
        logger.info("Node agent already listening on port %d", args.port)
        if not args.no_browser:
            _open_desktop_ui()
            return
        logger.error(
            "Port %d is already in use — refusing second headless instance",
            args.port,
        )
        sys.exit(0)

    def _sigterm(signum, frame):
        logger.info("SIGTERM received — shutting down")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm)

    import src.dashboard as dashboard
    dashboard.launch(port=args.port)


def _run_mcp_server() -> None:
    """Serve the MCP tool interface over stdio.

    stdout is the JSON-RPC transport here, so a single stray print would
    corrupt the stream and the client would drop the connection with a parse
    error that points nowhere useful. Setup therefore runs with stdout
    redirected to stderr, and logging goes to the rotating file as usual --
    only the protocol is allowed to write to the real stdout.

    This shares the agent's process model but not its port: the MCP server
    talks to the already-running agent over 127.0.0.1:5173 like any other
    client, so running it never competes for the dashboard port.
    """
    import contextlib

    with contextlib.redirect_stdout(sys.stderr):
        _setup_service_logging()
        # Logging must not reach stdout either; the file handler installed
        # above plus stderr is enough, and basicConfig would add a stdout one.
        from telescope_mcp.local_server import build_server
        server = build_server()

    server.run("stdio")


def _setup_service_logging() -> None:
    """Write logs to a rotating file alongside the data directory."""
    log_dir = pathlib.Path("logs")
    try:
        log_dir.mkdir(exist_ok=True)
        import logging.handlers
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "node_agent.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.root.addHandler(handler)
    except OSError:
        pass


if __name__ == "__main__":
    main()
