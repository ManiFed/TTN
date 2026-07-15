#!/usr/bin/env python3
"""
NodeSupervisor — keeps an unattended node observable and observing.

The dashboard process has many long-lived subsystems that historically only
started (or recovered) when a human clicked something in the browser UI.  A
headless service restart at 2 a.m. left the telescope disconnected until a
person intervened.  This supervisor closes those gaps with one periodic loop:

* **Device connection** — when no telescope/camera is connected and a default
  ALPACA server is saved in config, retry the connection with backoff.  This
  makes "power blip → service restart → observing resumes" true headless.
* **Image watcher** — re-mount the Seestar SMB share and restart the watcher
  if the watch path vanished (Seestar reboot, stale CIFS mount).
* **Disk health** — emit telemetry when free space is low and prune old
  artifacts (raw FITS, dashboard PNGs, exports) beyond the retention window.
* **Host sleep detection** — a jump between wall-clock and monotonic time
  means the machine slept; that's a critical event for an overnight node.

All hooks are injected callables so the supervisor is testable without a
Flask app or real hardware (see tests/gauntlet/test_supervisor.py).
"""

from __future__ import annotations

import logging
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from src import telemetry

logger = logging.getLogger("node_supervisor")

# Free-space thresholds (GB)
DISK_WARN_GB = 5.0
DISK_CRIT_GB = 1.0

# Reconnect backoff: 30 s → 60 → 120 → … capped at 10 min
_RECONNECT_BASE_S = 30.0
_RECONNECT_MAX_S = 600.0

# Wall-vs-monotonic divergence beyond this means the host slept
_SLEEP_GAP_S = 90.0


class NodeSupervisor:
    def __init__(
        self,
        load_config: Callable[[], dict],
        devices_connected: Callable[[], bool],
        connect_default: Callable[[str, int], bool],
        watcher_ok: Callable[[], bool],
        restart_watcher: Callable[[], bool],
        interval_s: float = 30.0,
    ) -> None:
        self._load_config = load_config
        self._devices_connected = devices_connected
        self._connect_default = connect_default
        self._watcher_ok = watcher_ok
        self._restart_watcher = restart_watcher
        self._interval_s = interval_s

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._reconnect_backoff_s = _RECONNECT_BASE_S
        self._next_reconnect_at = 0.0        # monotonic
        self._next_watcher_check_at = 0.0    # monotonic
        self._next_disk_check_at = 0.0       # monotonic
        self._last_wall = time.time()
        self._last_mono = time.monotonic()
        self._disk_state = "ok"              # ok | warn | crit

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="node-supervisor")
        self._thread.start()
        logger.info("Node supervisor started (interval %.0fs)", self._interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                # The supervisor is the last line of defence — it must never die.
                logger.error("Supervisor tick failed: %s", exc)
            self._stop.wait(self._interval_s)

    # ── One supervision pass (public for tests) ───────────────────────────────

    def tick(self, now_mono: Optional[float] = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        self._check_host_sleep()
        self._check_device_connection(now)
        self._check_image_watcher(now)
        self._check_disk(now)

    # ── Host sleep detection ───────────────────────────────────────────────────

    def _check_host_sleep(self) -> None:
        wall, mono = time.time(), time.monotonic()
        wall_dt = wall - self._last_wall
        mono_dt = mono - self._last_mono
        gap = wall_dt - mono_dt
        self._last_wall, self._last_mono = wall, mono
        if gap > _SLEEP_GAP_S:
            telemetry.event(
                "host_slept", severity="critical",
                detail={"gap_s": round(gap, 1),
                        "hint": "host machine slept or clock jumped — "
                                "check power settings / sleep prevention"})

    # ── Device connection ──────────────────────────────────────────────────────

    def _check_device_connection(self, now: float) -> None:
        if self._devices_connected():
            self._reconnect_backoff_s = _RECONNECT_BASE_S
            return
        if now < self._next_reconnect_at:
            return

        cfg = self._load_config()
        default = (cfg.get("alpaca") or {}).get("default_server") or {}
        host = str(default.get("address") or "")
        port = int(default.get("port") or 0)
        if not host or not port:
            return  # nothing saved to reconnect to — first-run, UI will handle it

        self._next_reconnect_at = now + self._reconnect_backoff_s
        self._reconnect_backoff_s = min(
            self._reconnect_backoff_s * 2, _RECONNECT_MAX_S)

        logger.info("Supervisor: devices disconnected — trying saved server %s:%d",
                    host, port)
        try:
            ok = self._connect_default(host, port)
        except Exception as exc:
            ok = False
            logger.warning("Supervisor: reconnect raised: %s", exc)
        if ok:
            self._reconnect_backoff_s = _RECONNECT_BASE_S
            telemetry.event("device_reconnected", severity="info",
                            detail={"host": host, "port": port})
        else:
            telemetry.event(
                "device_connect_failed", severity="warning",
                detail={"host": host, "port": port,
                        "retry_in_s": int(self._reconnect_backoff_s)})

    # ── Image watcher ──────────────────────────────────────────────────────────

    def _check_image_watcher(self, now: float) -> None:
        if now < self._next_watcher_check_at:
            return
        self._next_watcher_check_at = now + 120.0
        cfg = self._load_config()
        if not (cfg.get("image_watcher") or {}).get("enabled", False):
            return
        if self._watcher_ok():
            return
        logger.warning("Supervisor: image watcher unhealthy — restarting")
        try:
            ok = self._restart_watcher()
        except Exception as exc:
            ok = False
            logger.warning("Supervisor: watcher restart raised: %s", exc)
        telemetry.event(
            "image_watcher_restarted" if ok else "image_watcher_down",
            severity="info" if ok else "error",
            detail={"watch_path": (cfg.get("image_watcher") or {}).get("watch_path", "")})

    # ── Disk health & retention ────────────────────────────────────────────────

    def _check_disk(self, now: float) -> None:
        if now < self._next_disk_check_at:
            return
        self._next_disk_check_at = now + 3600.0

        free = telemetry.disk_free_gb(".")
        if free is not None:
            state = ("crit" if free < DISK_CRIT_GB
                     else "warn" if free < DISK_WARN_GB else "ok")
            if state != "ok" and state != self._disk_state:
                telemetry.event(
                    "disk_low", severity="critical" if state == "crit" else "warning",
                    detail={"free_gb": free})
            self._disk_state = state

        cfg = self._load_config()
        retention_days = float(
            (cfg.get("storage") or {}).get("retention_days", 14))
        if retention_days <= 0:
            return
        pruned = prune_old_files(retention_days=retention_days)
        if pruned:
            telemetry.event("retention_pruned", severity="info",
                            detail={"files": pruned,
                                    "retention_days": retention_days})


# Directories that accumulate per-frame artifacts on an unattended node.
_PRUNE_DIRS = (
    Path("data") / "fits",
    Path("data") / "images",
    Path("fits_export"),
    Path("aavso_submissions"),
)


def prune_old_files(retention_days: float, dirs=_PRUNE_DIRS,
                    now: Optional[float] = None) -> int:
    """Delete regular files older than the retention window. Returns count."""
    cutoff = (now or time.time()) - retention_days * 86400.0
    protected = _pending_science_files()
    removed = 0
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                try:
                    if (f.is_file() and f.resolve() not in protected
                            and f.stat().st_mtime < cutoff):
                        f.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            continue
    if removed:
        logger.info("Retention sweep removed %d file(s) older than %.0f days",
                    removed, retention_days)
    return removed


def _pending_science_files(path: Path = Path("data") / "node_outbox.db") -> set[Path]:
    """Raw FITS referenced by unacknowledged SQLite payloads are never pruned."""
    if not path.exists():
        return set()
    out: set[Path] = set()
    try:
        conn = sqlite3.connect(str(path), timeout=2)
        try:
            rows = conn.execute("SELECT payload FROM outbox WHERE priority>=90").fetchall()
        finally:
            conn.close()
        for (raw,) in rows:
            payload = json.loads(raw)
            candidate = payload.get("_raw_fits_path")
            if not candidate and isinstance(payload.get("frame"), dict):
                candidate = payload["frame"].get("_raw_fits_path")
            if candidate:
                out.add(Path(candidate).resolve())
    except Exception:
        return set()
    return out
