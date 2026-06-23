#!/usr/bin/env python3
"""
Cross-platform OS sleep prevention for the The Telescope Net Node Agent.

The node agent runs overnight.  If the host computer sleeps, the Seestar
loses its connection and the night is wasted.  This module prevents that.

    from sleep_prevention import enable, disable

Call enable() once at startup.  disable() releases the assertion (called at
shutdown, but the OS also cleans up on process exit anyway).

Platform behaviour
------------------
Windows  SetThreadExecutionState — tells Windows the thread requires the
         system to stay awake and the display can still sleep.  No external
         tools required.

macOS    Launches `caffeinate -s -w <PID>` as a background process.
         caffeinate exits automatically when the parent process does.
         Prevents idle sleep; allows display sleep.

Linux    Attempts `systemd-inhibit` first (most modern distros).
         Falls back to a best-effort `xdg-screensaver suspend` loop if
         systemd-inhibit is not available.  If neither works, logs a
         warning so the operator knows to configure sleep manually.
"""

import logging
import os
import platform
import subprocess
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger("sleep_prevention")

_caffeinate_proc: Optional[subprocess.Popen] = None
_inhibit_proc: Optional[subprocess.Popen] = None
_xdg_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def enable() -> None:
    """Prevent the OS from sleeping.  Safe to call multiple times."""
    system = platform.system()
    if system == "Windows":
        _enable_windows()
    elif system == "Darwin":
        _enable_macos()
    elif system == "Linux":
        _enable_linux()
    else:
        logger.warning(
            "Sleep prevention not implemented for %s — "
            "configure power settings manually to prevent overnight sleep", system)


def disable() -> None:
    """Release sleep prevention.  Called at clean shutdown."""
    global _caffeinate_proc, _inhibit_proc
    _stop_event.set()

    if _caffeinate_proc is not None:
        try:
            _caffeinate_proc.terminate()
        except OSError:
            pass
        _caffeinate_proc = None

    if _inhibit_proc is not None:
        try:
            _inhibit_proc.terminate()
        except OSError:
            pass
        _inhibit_proc = None

    system = platform.system()
    if system == "Windows":
        try:
            import ctypes
            # ES_CONTINUOUS only — release the previous request
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        except Exception:
            pass

    logger.info("Sleep prevention released")


# ── Platform implementations ───────────────────────────────────────────────────

def _enable_windows() -> None:
    try:
        import ctypes
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        result = ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
        if result == 0:
            logger.warning("SetThreadExecutionState failed — sleep not prevented")
        else:
            logger.info("Sleep prevention active (Windows SetThreadExecutionState)")
    except Exception as exc:
        logger.warning("Could not enable Windows sleep prevention: %s", exc)


def _enable_macos() -> None:
    global _caffeinate_proc
    try:
        _caffeinate_proc = subprocess.Popen(
            ["caffeinate", "-s", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Sleep prevention active (caffeinate PID %d)", _caffeinate_proc.pid)
    except FileNotFoundError:
        logger.warning("caffeinate not found — sleep not prevented (unusual on macOS)")
    except Exception as exc:
        logger.warning("Could not start caffeinate: %s", exc)


def _enable_linux() -> None:
    global _inhibit_proc, _xdg_thread

    # Try systemd-inhibit first (available on most modern Linux distros)
    try:
        # systemd-inhibit wraps a sleeping child process; the inhibit lock is
        # held for as long as the child lives.
        _inhibit_proc = subprocess.Popen(
            [
                "systemd-inhibit",
                "--what=idle:sleep",
                "--who=The Telescope Net Node Agent",
                "--why=Overnight telescope observation",
                "--mode=block",
                sys.executable, "-c",
                "import signal, time; signal.pause()",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(
            "Sleep prevention active (systemd-inhibit PID %d)", _inhibit_proc.pid)
        return
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("systemd-inhibit failed: %s", exc)

    # Fall back: xdg-screensaver suspend loop (X11 / Wayland limited)
    try:
        subprocess.run(["xdg-screensaver", "--version"],
                       capture_output=True, check=True, timeout=3)

        def _xdg_loop():
            window_id = _get_x11_window_id()
            while not _stop_event.is_set():
                if window_id:
                    subprocess.run(["xdg-screensaver", "suspend", window_id],
                                   capture_output=True, timeout=5)
                _stop_event.wait(50)

        _xdg_thread = threading.Thread(target=_xdg_loop, daemon=True,
                                       name="sleep-prevention-xdg")
        _xdg_thread.start()
        logger.info("Sleep prevention active (xdg-screensaver loop)")
        return
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    logger.warning(
        "Sleep prevention unavailable (no systemd-inhibit or xdg-screensaver found). "
        "Configure your system to prevent sleep: "
        "sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target"
    )


def _get_x11_window_id() -> Optional[str]:
    """Try to get an X11 window ID to pass to xdg-screensaver."""
    try:
        result = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None
