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
import signal
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

logger = logging.getLogger("main_service")


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
        os.chdir(args.data_dir)

    # On Windows as a service, stdout may not exist — redirect to a log file
    _setup_service_logging()

    if args.no_browser:
        # Monkey-patch webbrowser so the service doesn't try to open a tab
        import webbrowser
        webbrowser.open = lambda *a, **kw: None  # type: ignore[assignment]

    # Install a SIGTERM handler so systemd / launchd can shut us down cleanly
    def _sigterm(signum, frame):
        logger.info("SIGTERM received — shutting down")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm)

    # Import and run the dashboard (this blocks until Ctrl-C / SIGTERM)
    import src.dashboard as dashboard
    dashboard.launch(port=args.port)


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
