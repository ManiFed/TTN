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
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

logger = logging.getLogger("main_service")


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
        # The placeholder is useful to installers but is not a real code.
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
            "  activation_code: ''\n",
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


def _open_dashboard(port: int) -> None:
    url = f"http://localhost:{port}"
    try:
        webbrowser.open(url)
    except Exception as exc:
        logger.warning("Could not open browser for %s: %s", url, exc)


def _macos_gui_launch(port: int, data_dir: pathlib.Path) -> bool:
    """Handle double-click / Finder launch of the .app bundle.

    A PyInstaller binary is not a Cocoa app, so if it stays in the foreground
    Launch Services marks it "Application Not Responding" forever and the
    process holds a lock that blocks deleting the .app.

    Strategy: if the dashboard is not up, spawn a detached daemon child, wait
    for the port, open the browser, then exit the GUI-tracked process.
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

    _open_dashboard(port)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="The Telescope Net Node Agent")
    parser.add_argument("--port", type=int, default=5173,
                        help="Dashboard port (default: 5173)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Start in headless mode without opening a browser tab")
    parser.add_argument("--data-dir", default="",
                        help="Working directory for config.yaml and data/ (default: current dir)")
    args = parser.parse_args()

    if args.data_dir:
        data_dir = pathlib.Path(args.data_dir).expanduser()
    elif getattr(sys, "frozen", False):
        # Finder/DMG launches do not inherit a useful working directory. Never
        # try to persist config beside the signed app or on a read-only DMG.
        data_dir = _default_data_dir()
    else:
        data_dir = pathlib.Path.cwd()
    _prepare_data_dir(data_dir)
    os.chdir(data_dir)

    # On Windows as a service, stdout may not exist — redirect to a log file
    _setup_service_logging()

    # Finder double-click: detach daemon + open browser + exit (no ANR).
    if not args.no_browser and _macos_gui_launch(args.port, data_dir):
        return

    # Already running (e.g. launchd service up, second click / second start).
    if _port_open(args.port):
        logger.info("Dashboard already listening on port %d", args.port)
        if not args.no_browser:
            _open_dashboard(args.port)
            return
        # Headless/service restarts should not stack a second server.
        logger.error(
            "Port %d is already in use — refusing second headless instance",
            args.port,
        )
        sys.exit(0)

    # Install a SIGTERM handler so systemd / launchd can shut us down cleanly
    def _sigterm(signum, frame):
        logger.info("SIGTERM received — shutting down")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm)

    # Import and run the dashboard (this blocks until Ctrl-C / SIGTERM)
    import src.dashboard as dashboard
    dashboard.launch(port=args.port, open_browser=not args.no_browser)


def _setup_service_logging() -> None:
    """Write logs to a rotating file alongside the data directory."""
    log_dir = pathlib.Path("logs")
    try:
        log_dir.mkdir(exist_ok=True)
        import logging.handlers
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "node_agent.log",
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.root.addHandler(handler)
    except OSError:
        pass  # Console logging still works


if __name__ == "__main__":
    main()
