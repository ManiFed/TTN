#!/usr/bin/env python3
"""
Structured reliability telemetry for the Node Agent.

Every operationally significant event (emergency park, slew failure, plan
received/skipped, registration failure, watcher restart, …) is recorded as a
structured event so a remote operator can diagnose a failed night without
asking the member to read raw logs.

Each event goes to three places:

1. ``logs/events.jsonl`` — append-only JSON-lines file (size-capped) that
   survives restarts and can be attached to a support request.
2. An in-memory ring buffer served by the dashboard (``/api/events``) and
   summarised into every cloud heartbeat (``conditions.events``), so the
   cloud sees recent failures even when only heartbeats get through.
3. An optional *forwarder* (set by the dashboard once the cloud communicator
   exists) that pushes ``error``/``critical`` events to the cloud incident
   API in a background thread — best-effort, never blocking the caller.

Usage:
    from src import telemetry
    telemetry.event("slew_failed", severity="error",
                    target="V1234 Cyg", detail={"timeout_s": 180})
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("telemetry")

_SEVERITIES = ("debug", "info", "warning", "error", "critical")

_EVENTS_FILE = Path("logs") / "events.jsonl"
_EVENTS_FILE_MAX_BYTES = 5 * 1024 * 1024   # rotate to .1 beyond this
_RING_SIZE = 200

_lock = threading.Lock()
_ring: deque = deque(maxlen=_RING_SIZE)
_counters: dict[str, int] = {}
_started_monotonic = time.monotonic()

# Cloud forwarding — a bounded queue drained by one daemon thread so a cloud
# outage can never block or pile threads onto the code path that emitted the
# event (which may itself be the cloud communicator).
_forwarder: Optional[Callable[[dict], None]] = None
_forward_queue: "queue.Queue[dict]" = queue.Queue(maxsize=100)
_forward_thread: Optional[threading.Thread] = None


def event(name: str, severity: str = "info",
          target: str = "", detail: Optional[dict[str, Any]] = None) -> dict:
    """Record one structured event. Never raises."""
    if severity not in _SEVERITIES:
        severity = "info"
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "unix": round(time.time(), 3),
        "event": str(name)[:80],
        "severity": severity,
        "target": str(target)[:160],
        "detail": detail or {},
    }
    with _lock:
        _ring.append(entry)
        _counters[entry["event"]] = _counters.get(entry["event"], 0) + 1
    _append_jsonl(entry)
    if severity in ("error", "critical"):
        _enqueue_forward(entry)
    # Mirror into the normal log stream so the dashboard console shows it too.
    log_fn = getattr(logger, severity if severity != "critical" else "critical",
                     logger.info)
    log_fn("event %s%s %s", name, f" [{target}]" if target else "",
           json.dumps(detail or {}, default=str)[:400])
    return entry


def recent(n: int = 50, min_severity: str = "debug") -> list[dict]:
    """Most recent events, newest last."""
    try:
        floor = _SEVERITIES.index(min_severity)
    except ValueError:
        floor = 0
    with _lock:
        out = [e for e in _ring if _SEVERITIES.index(e["severity"]) >= floor]
    return out[-n:]


def counters() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def heartbeat_summary(max_events: int = 5) -> dict:
    """Compact evidence block embedded in every cloud heartbeat.

    Kept small on purpose: the most recent warning-or-worse events (name,
    severity, age) plus lifetime counters, so a remote operator sees *why* a
    night failed from the nodes table alone.
    """
    now = time.time()
    events = [
        {
            "event": e["event"],
            "severity": e["severity"],
            "target": e["target"],
            "age_s": int(now - e["unix"]),
        }
        for e in recent(max_events, min_severity="warning")
    ]
    disk = disk_free_gb()
    out = {
        "recent": events,
        "counters": counters(),
        "uptime_s": int(time.monotonic() - _started_monotonic),
    }
    if disk is not None:
        out["disk_free_gb"] = disk
    return out


def disk_free_gb(path: str = ".") -> Optional[float]:
    try:
        import shutil
        return round(shutil.disk_usage(path).free / 1e9, 2)
    except Exception:
        return None


# ── Cloud forwarding ───────────────────────────────────────────────────────────

def set_forwarder(fn: Callable[[dict], None]) -> None:
    """Install the cloud incident forwarder (called once from launch())."""
    global _forwarder, _forward_thread
    _forwarder = fn
    if _forward_thread is None or not _forward_thread.is_alive():
        _forward_thread = threading.Thread(
            target=_forward_loop, daemon=True, name="telemetry-forward")
        _forward_thread.start()


def _enqueue_forward(entry: dict) -> None:
    if _forwarder is None:
        return
    try:
        _forward_queue.put_nowait(entry)
    except queue.Full:
        pass  # cloud unreachable and backlog full — heartbeat summary still carries it


def _forward_loop() -> None:
    while True:
        entry = _forward_queue.get()
        fn = _forwarder
        if fn is None:
            continue
        try:
            fn(entry)
        except Exception as exc:
            logger.debug("Event forward failed (%s): %s", entry.get("event"), exc)


# ── JSONL persistence ──────────────────────────────────────────────────────────

def _append_jsonl(entry: dict) -> None:
    try:
        _EVENTS_FILE.parent.mkdir(exist_ok=True)
        if (_EVENTS_FILE.exists()
                and _EVENTS_FILE.stat().st_size > _EVENTS_FILE_MAX_BYTES):
            rotated = _EVENTS_FILE.with_suffix(".jsonl.1")
            rotated.unlink(missing_ok=True)
            _EVENTS_FILE.rename(rotated)
        with open(_EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass  # never let evidence writing break the operation being evidenced


def load_events_file(n: int = 500) -> list[dict]:
    """Read the last *n* persisted events (for /api/events?source=disk)."""
    out: list[dict] = []
    try:
        lines = _EVENTS_FILE.read_text(encoding="utf-8").splitlines()[-n:]
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def reset_for_tests() -> None:
    """Clear in-memory state (ring, counters). Test helper only."""
    global _forwarder
    with _lock:
        _ring.clear()
        _counters.clear()
    _forwarder = None
