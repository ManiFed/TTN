#!/usr/bin/env python3
"""
Web dashboard for monitoring the ALPACA telescope control script.

Run:  python dashboard.py
Then open http://localhost:5000 in a browser.
"""

import base64
import io
import json
import logging
import queue
import sys
import threading
import time
from typing import Any, Optional

import yaml
from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context

from alpaca.client import AlpacaError
from alpaca.discovery import discover_servers
from alpaca.safety_manager import SafetyManager
from alpaca.telescope import Telescope
from alpaca.camera import Camera


# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ── Shared state ───────────────────────────────────────────────────────────────

_state: dict[str, Any] = {
    "server":         None,   # {"address": str, "port": int} | None
    "connected":      False,
    "phase":          "idle", # idle | discovering | connecting | unpark | tracking_on |
                              # verify_movement | slew | hold | expose | park | done | error
    "telescope": {
        "enabled":   False,
        "connected": False,
        "slewing":   None,
        "parked":    None,
        "tracking":  None,
        "ra":        None,
        "dec":       None,
    },
    "camera": {
        "enabled":     False,
        "connected":   False,
        "state":       None,
        "state_name":  None,
        "image_ready": None,
    },
    "safety": {
        "safe":              True,
        "parked":            False,
        "reason":            "",
        "heartbeat_ok":      True,
        "disconnected_secs": None,
        "sun_elevation":     None,
        "dawn_threshold":    -18.0,
    },
    "hold_remaining":  None,
    "error":           None,
    "run_active":      False,
    "image_captured":  False,
}
_state_lock = threading.Lock()

_CAMERA_STATES = {
    0: "Idle", 1: "Waiting", 2: "Exposing",
    3: "Reading", 4: "Downloading", 5: "Error",
}


# ── Log broadcasting ───────────────────────────────────────────────────────────

_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()
_log_history: list[dict] = []


def _broadcast(entry: dict) -> None:
    global _log_history
    _log_history.append(entry)
    if len(_log_history) > 300:
        _log_history = _log_history[-300:]
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(entry)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


class _BroadcastHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        _broadcast({
            "time":  time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "name":  record.name,
            "msg":   self.format(record),
        })


logging.getLogger().addHandler(_BroadcastHandler())
logger = logging.getLogger("dashboard")


# ── Stdout capture (catches print() calls in verify_movement etc.) ─────────────

class _StdoutCapture:
    _orig = sys.__stdout__

    def write(self, text: str) -> None:
        text = text.strip()
        if text:
            logging.getLogger("stdout").info(text)

    def flush(self) -> None:
        pass


sys.stdout = _StdoutCapture()  # type: ignore[assignment]


# ── Device handles (set by poller/sequence, read by both) ─────────────────────

_tel: Optional[Telescope] = None
_cam: Optional[Camera] = None

# ── Captured image (base64-encoded PNG, set after expose) ─────────────────────

_last_image_b64: Optional[str] = None
_last_image_lock = threading.Lock()


def _capture_image() -> None:
    """Download the latest image from the camera, convert to PNG, and store as base64."""
    global _last_image_b64
    if _cam is None:
        return
    try:
        import numpy as np
        from PIL import Image

        logger.info("Downloading image array from camera…")
        raw = _cam.image_array()
        arr = np.array(raw, dtype=np.float32)

        # ALPACA returns shape (planes, height, width) for color or (height, width) for mono.
        if arr.ndim == 3 and arr.shape[0] in (1, 3):
            arr = np.transpose(arr, (1, 2, 0))
            if arr.shape[2] == 1:
                arr = arr[:, :, 0]

        # Stretch to 8-bit
        mn, mx = float(arr.min()), float(arr.max())
        if mx > mn:
            arr = (arr - mn) / (mx - mn) * 255.0
        arr = arr.clip(0, 255).astype(np.uint8)

        mode = "RGB" if arr.ndim == 3 else "L"
        img = Image.fromarray(arr, mode=mode)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        with _last_image_lock:
            _last_image_b64 = b64
        with _state_lock:
            _state["image_captured"] = True
        logger.info("Image stored — %.1f KB PNG", len(b64) * 3 / 4 / 1024)
    except Exception as exc:
        logger.error("Image capture failed: %s", exc)


# ── Safety manager (created in launch(), telescope attached after connect) ─────

_safety_mgr: Optional[SafetyManager] = None


def _on_safety_unsafe() -> None:
    """Called by SafetyManager when the system becomes unsafe."""
    _run_abort.set()
    reason = _safety_mgr.status()["reason"] if _safety_mgr else "unknown"
    with _state_lock:
        _state["phase"] = "error"
        _state["error"] = f"Safety stop: {reason}"
    logger.critical("Safety manager triggered abort: %s", reason)


# ── Background poller ──────────────────────────────────────────────────────────

_poller_stop = threading.Event()


def _poll_loop() -> None:
    while not _poller_stop.is_set():
        with _state_lock:
            tel_enabled = _state["telescope"]["enabled"]
            cam_enabled = _state["camera"]["enabled"]

        if tel_enabled and _tel is not None:
            try:
                ra       = _tel.ra()
                dec      = _tel.dec()
                slewing  = _tel.is_slewing()
                parked   = _tel.is_parked()
                tracking = _tel.is_tracking()
                with _state_lock:
                    _state["telescope"].update(
                        connected=True, ra=ra, dec=dec,
                        slewing=slewing, parked=parked, tracking=tracking,
                    )
            except Exception:
                with _state_lock:
                    _state["telescope"]["connected"] = False

        if cam_enabled and _cam is not None:
            try:
                state     = _cam.camera_state()
                img_ready = _cam.image_ready()
                with _state_lock:
                    _state["camera"].update(
                        connected=True, state=state,
                        state_name=_CAMERA_STATES.get(state, "Unknown"),
                        image_ready=img_ready,
                    )
            except Exception:
                with _state_lock:
                    _state["camera"]["connected"] = False

        if _safety_mgr is not None:
            try:
                safety_snap = _safety_mgr.status()
                with _state_lock:
                    _state["safety"].update(safety_snap)
            except Exception:
                pass

        time.sleep(1.0)


def _start_poller() -> None:
    _poller_stop.clear()
    t = threading.Thread(target=_poll_loop, daemon=True, name="alpaca-poller")
    t.start()


# ── Sequence runner ────────────────────────────────────────────────────────────

_run_abort = threading.Event()


def _phase(name: str) -> None:
    with _state_lock:
        _state["phase"] = name
    logger.info("── Phase: %s", name)


def _run_sequence(cfg: dict) -> None:
    global _tel, _cam
    try:
        with _state_lock:
            _state.update(run_active=True, error=None, phase="idle", image_captured=False)

        alpaca_cfg  = cfg.get("alpaca", {})
        devices_cfg = cfg.get("devices", {})
        api_ver     = alpaca_cfg.get("api_version", 1)

        # Discovery
        _phase("discovering")
        servers = discover_servers(
            port=alpaca_cfg.get("discovery_port", 32227),
            timeout=alpaca_cfg.get("discovery_timeout", 5),
        )
        if not servers:
            raise RuntimeError("No ALPACA servers found on LAN")
        if _run_abort.is_set():
            return

        server = servers[0]
        with _state_lock:
            _state["server"] = server

        # Connect devices
        _phase("connecting")

        if devices_cfg.get("telescope", {}).get("enabled", False):
            num = devices_cfg["telescope"].get("device_number", 0)
            _tel = Telescope(server["address"], server["port"], num, api_ver)
            _tel.connect()
            with _state_lock:
                _state["telescope"].update(enabled=True, connected=True)
            if _safety_mgr is not None:
                _safety_mgr.attach_telescope(_tel)

        if devices_cfg.get("camera", {}).get("enabled", False):
            num = devices_cfg["camera"].get("device_number", 0)
            _cam = Camera(server["address"], server["port"], num, api_ver)
            _cam.connect()
            with _state_lock:
                _state["camera"].update(enabled=True, connected=True)

        with _state_lock:
            _state["connected"] = True

        _start_poller()

        if _run_abort.is_set():
            return

        # ── Telescope sequence ─────────────────────────────────────────────────
        if _tel is not None:
            tel_cfg = cfg.get("telescope", {})

            _phase("unpark")
            _tel.unpark()
            if _run_abort.is_set(): return

            _phase("tracking_on")
            _tel.set_tracking(True)
            if _run_abort.is_set(): return

            _phase("verify_movement")
            _tel.verify_movement()
            if _run_abort.is_set(): return

            _phase("slew")
            _tel.slew_to_coordinates(
                ra=tel_cfg.get("slew_ra", 0.0),
                dec=tel_cfg.get("slew_dec", 0.0),
            )
            if _run_abort.is_set(): return

            _phase("hold")
            logger.info("Holding at destination for 3 minutes — check your pier cam…")
            hold_end = time.monotonic() + 180
            while time.monotonic() < hold_end and not _run_abort.is_set():
                remaining = max(0, int(hold_end - time.monotonic()))
                with _state_lock:
                    _state["hold_remaining"] = remaining
                time.sleep(1)
            with _state_lock:
                _state["hold_remaining"] = None
            logger.info("Hold complete, continuing.")
            if _run_abort.is_set(): return

        # ── Camera sequence ────────────────────────────────────────────────────
        if _cam is not None:
            cam_cfg  = cfg.get("camera", {})
            binning  = cam_cfg.get("binning", 1)
            duration = cam_cfg.get("exposure_duration", 1.0)

            _phase("expose")
            _cam.set_binning(binning)
            _cam.expose(duration=duration, light=True)
            if _run_abort.is_set(): return
            _capture_image()

        # ── Park ───────────────────────────────────────────────────────────────
        if _tel is not None:
            _phase("park")
            _tel.park()

        _phase("done")
        logger.info("Sequence complete.")

    except Exception as exc:
        logger.exception("Sequence error: %s", exc)
        with _state_lock:
            _state["phase"] = "error"
            _state["error"] = str(exc)
    finally:
        with _state_lock:
            _state["run_active"] = False
        if _run_abort.is_set():
            logger.warning("Sequence aborted by user.")
            with _state_lock:
                _state["phase"] = "idle"
                _state["hold_remaining"] = None


# ── Config helper ──────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        with open("config.yaml") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        return {}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(_HTML)


@app.route("/api/status")
def api_status():
    with _state_lock:
        return jsonify(dict(_state))


@app.route("/api/logs")
def api_logs():
    """Server-Sent Events stream of log records."""
    q: queue.Queue = queue.Queue(maxsize=400)
    for entry in _log_history:
        try:
            q.put_nowait(entry)
        except queue.Full:
            break
    with _subscribers_lock:
        _subscribers.append(q)

    def generate():
        try:
            while True:
                try:
                    entry = q.get(timeout=15)
                    yield f"data: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _subscribers_lock:
                try:
                    _subscribers.remove(q)
                except ValueError:
                    pass

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/discover", methods=["POST"])
def api_discover():
    cfg = _load_config()
    alpaca_cfg = cfg.get("alpaca", {})
    logger.info("Starting LAN discovery…")
    servers = discover_servers(
        port=alpaca_cfg.get("discovery_port", 32227),
        timeout=alpaca_cfg.get("discovery_timeout", 5),
    )
    return jsonify({"servers": servers})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    global _tel, _cam
    data       = request.get_json(force=True) or {}
    host       = data.get("host", "")
    port       = int(data.get("port", 11111))
    cfg        = _load_config()
    api_ver    = cfg.get("alpaca", {}).get("api_version", 1)
    devices    = cfg.get("devices", {})

    logger.info("Connecting to ALPACA server %s:%d", host, port)
    with _state_lock:
        _state["server"]    = {"address": host, "port": port}
        _state["connected"] = False

    if devices.get("telescope", {}).get("enabled", False):
        num = devices["telescope"].get("device_number", 0)
        try:
            _tel = Telescope(host, port, num, api_ver)
            _tel.connect()
            with _state_lock:
                _state["telescope"].update(enabled=True, connected=True)
            if _safety_mgr is not None:
                _safety_mgr.attach_telescope(_tel)
        except Exception as exc:
            logger.error("Telescope connect failed: %s", exc)

    if devices.get("camera", {}).get("enabled", False):
        num = devices["camera"].get("device_number", 0)
        try:
            _cam = Camera(host, port, num, api_ver)
            _cam.connect()
            with _state_lock:
                _state["camera"].update(enabled=True, connected=True)
        except Exception as exc:
            logger.error("Camera connect failed: %s", exc)

    with _state_lock:
        _state["connected"] = True

    _start_poller()
    return jsonify({"ok": True})


@app.route("/api/run", methods=["POST"])
def api_run():
    with _state_lock:
        if _state["run_active"]:
            return jsonify({"error": "Already running"}), 409
    _run_abort.clear()
    cfg = _load_config()
    t = threading.Thread(
        target=_run_sequence, args=(cfg,), daemon=True, name="sequence"
    )
    t.start()
    return jsonify({"ok": True})


@app.route("/api/abort", methods=["POST"])
def api_abort():
    _run_abort.set()
    logger.warning("Abort requested by user.")
    return jsonify({"ok": True})


@app.route("/api/slew", methods=["POST"])
def api_slew():
    global _tel
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400
    data = request.get_json(force=True) or {}
    try:
        ra  = float(data["ra"])
        dec = float(data["dec"])
    except (KeyError, ValueError):
        return jsonify({"error": "Invalid ra/dec"}), 400
    if not (0.0 <= ra < 24.0):
        return jsonify({"error": "RA must be in range [0, 24)"}), 400
    if not (-90.0 <= dec <= 90.0):
        return jsonify({"error": "Dec must be in range [-90, 90]"}), 400

    def _do_slew():
        try:
            _tel.slew_to_coordinates(ra=ra, dec=dec)
        except Exception as exc:
            logger.error("Manual slew failed: %s", exc)

    threading.Thread(target=_do_slew, daemon=True, name="manual-slew").start()
    logger.info("Manual slew commanded: RA=%.4f h  Dec=%.4f °", ra, dec)
    return jsonify({"ok": True})


@app.route("/api/safety")
def api_safety():
    if _safety_mgr is None:
        return jsonify({"enabled": False})
    return jsonify({"enabled": True, **_safety_mgr.status()})


@app.route("/api/image")
def api_image():
    with _last_image_lock:
        b64 = _last_image_b64
    if b64 is None:
        return jsonify({"error": "No image available"}), 404
    img_bytes = base64.b64decode(b64)
    return Response(img_bytes, content_type="image/png",
                    headers={"Cache-Control": "no-store"})


# ── HTML template ──────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NODE v1 — ALPACA Dashboard</title>
<style>
:root {
  --bg:       #070a0e;
  --surface:  #0d1117;
  --surface2: #161b22;
  --border:   #21262d;
  --green:    #3fb950;
  --green-hi: #56d364;
  --yellow:   #d29922;
  --red:      #f85149;
  --blue:     #58a6ff;
  --gray:     #484f58;
  --text:     #c9d1d9;
  --dim:      #8b949e;
  --mono:     'Courier New', 'Consolas', monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Header ── */
.hdr {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 10px 20px;
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
}
.hdr-logo  { font-size: 17px; font-weight: bold; color: var(--green-hi); letter-spacing: 3px; }
.hdr-sub   { color: var(--dim); font-size: 10px; letter-spacing: 2px; margin-top: 2px; }
.hdr-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.hdr-server { color: var(--dim); font-size: 12px; }
.hdr-server span { color: var(--blue); }

.conn-pill {
  display: flex; align-items: center; gap: 5px;
  font-size: 11px; letter-spacing: 1px;
}

/* ── Buttons ── */
.btn {
  padding: 4px 13px;
  border: 1px solid;
  background: transparent;
  font-family: var(--mono);
  font-size: 11px;
  cursor: pointer;
  letter-spacing: 1px;
  text-transform: uppercase;
  transition: background 0.12s, color 0.12s;
}
.btn-green { border-color: var(--green);  color: var(--green); }
.btn-green:hover:not(:disabled) { background: var(--green);  color: var(--bg); }
.btn-red   { border-color: var(--red);    color: var(--red); }
.btn-red:hover:not(:disabled)   { background: var(--red);    color: var(--bg); }
.btn-blue  { border-color: var(--blue);   color: var(--blue); }
.btn-blue:hover:not(:disabled)  { background: var(--blue);   color: var(--bg); }
.btn-dim   { border-color: var(--gray);   color: var(--dim); }
.btn-dim:hover:not(:disabled)   { background: var(--gray);   color: var(--text); }
.btn:disabled { opacity: 0.3; cursor: not-allowed; }

/* ── Dot indicators ── */
.dot {
  width: 8px; height: 8px; border-radius: 50%;
  display: inline-block; flex-shrink: 0;
}
.dot-green  { background: var(--green);  box-shadow: 0 0 6px var(--green); }
.dot-yellow { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
.dot-red    { background: var(--red);    box-shadow: 0 0 6px var(--red); }
.dot-gray   { background: var(--gray); }

@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.35; } }
.pulse { animation: pulse 1.1s ease-in-out infinite; }

/* ── Main layout ── */
.main {
  flex: 1;
  display: grid;
  grid-template-rows: auto 1fr auto;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--border);
  overflow: hidden;
  min-height: 0;
}

/* ── Log footer (pinned to bottom) ── */
.log-footer {
  flex-shrink: 0;
  height: 200px;
  background: var(--surface);
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

/* ── Sequence strip (full width, row 1) ── */
.seq-panel {
  grid-column: 1 / -1;
  background: var(--surface);
  padding: 12px 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.panel-label {
  font-size: 10px; letter-spacing: 2px; color: var(--dim);
  text-transform: uppercase;
}
.steps {
  display: flex; align-items: center; flex-wrap: wrap; gap: 3px 0;
}
.step-chip {
  padding: 3px 11px;
  border: 1px solid var(--gray);
  color: var(--gray);
  font-size: 10px; letter-spacing: 1px; text-transform: uppercase;
  transition: border-color .15s, color .15s;
}
.step-chip.active { border-color: var(--yellow); color: var(--yellow); }
.step-chip.done   { border-color: var(--green);  color: var(--green); }
.step-chip.error  { border-color: var(--red);    color: var(--red); }
.step-arrow { color: var(--gray); padding: 0 3px; font-size: 11px; }

.hold-bar {
  font-size: 13px; letter-spacing: 3px; color: var(--yellow);
  margin-top: 2px;
}
.err-text { font-size: 12px; color: var(--red); margin-top: 2px; }

/* ── Telescope & Camera panels (row 2) ── */
.panel {
  background: var(--surface);
  padding: 14px 20px;
  display: flex; flex-direction: column; gap: 10px;
  overflow: hidden; min-height: 0;
}
.panel-hdr {
  display: flex; align-items: center; justify-content: space-between;
  border-bottom: 1px solid var(--border); padding-bottom: 8px;
}
.panel-name {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; font-weight: bold; letter-spacing: 2px;
  text-transform: uppercase;
}
.badges { display: flex; gap: 5px; flex-wrap: wrap; }
.badge {
  padding: 2px 7px; font-size: 10px; letter-spacing: 1px;
  text-transform: uppercase; border: 1px solid var(--gray); color: var(--gray);
}
.badge-on   { border-color: var(--green);  color: var(--green); }
.badge-warn { border-color: var(--yellow); color: var(--yellow); }
.badge-err  { border-color: var(--red);    color: var(--red); }

/* ── Coordinates ── */
.coords { display: grid; grid-template-columns: 48px 1fr; gap: 5px 12px; align-items: center; }
.coord-lbl { color: var(--dim); font-size: 11px; text-align: right; }
.coord-val { font-size: 22px; color: var(--green-hi); letter-spacing: 2px; }
.coord-val.dim { color: var(--gray); }
.coord-raw { color: var(--dim); font-size: 11px; }

/* ── Camera state ── */
.cam-state { font-size: 26px; letter-spacing: 3px; }
.cs-idle   { color: var(--gray); }
.cs-wait   { color: var(--dim); }
.cs-expose { color: var(--yellow); }
.cs-read   { color: var(--blue); }
.cs-dl     { color: var(--blue); }
.cs-error  { color: var(--red); }
.cam-sub   { color: var(--dim); font-size: 11px; }

/* ── Image panel (row 3, full width) ── */
.img-panel {
  grid-column: 1 / -1;
  background: var(--surface);
  padding: 12px 20px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.img-panel.hidden { display: none; }
.img-inner {
  display: flex;
  align-items: flex-start;
  gap: 20px;
}
.img-frame {
  border: 1px solid var(--border);
  background: #000;
  flex-shrink: 0;
  max-width: 480px;
  position: relative;
}
.img-frame img {
  display: block;
  max-width: 480px;
  max-height: 320px;
  width: 100%;
  image-rendering: pixelated;
}
.img-meta {
  color: var(--dim);
  font-size: 11px;
  line-height: 1.8;
}
.img-meta span { color: var(--text); }

/* ── Log panel (inside footer) ── */
.log-panel {
  display: flex; flex-direction: column;
  overflow: hidden; min-height: 0;
  flex: 1;
}
.log-hdr {
  padding: 6px 20px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
  flex-shrink: 0;
}
.log-body {
  flex: 1; overflow-y: auto;
  padding: 5px 20px; font-size: 12px; line-height: 1.65;
}
.log-body::-webkit-scrollbar { width: 5px; }
.log-body::-webkit-scrollbar-track { background: var(--bg); }
.log-body::-webkit-scrollbar-thumb { background: var(--border); }

.ll { display: flex; gap: 8px; }
.lt  { color: var(--dim); flex-shrink:0; width:68px; }
.llv { flex-shrink:0; width:50px; }
.llv.INFO    { color: var(--green); }
.llv.WARNING { color: var(--yellow); }
.llv.ERROR   { color: var(--red); }
.llv.DEBUG   { color: var(--gray); }
.ln  { color: var(--blue); flex-shrink:0; min-width:110px; max-width:150px; overflow:hidden; }
.lm  { color: var(--text); word-break: break-all; }
.lm.warn-msg { color: var(--yellow); }
.lm.err-msg  { color: var(--red); }

.count-badge {
  font-size: 10px; color: var(--dim);
  padding: 1px 7px; border: 1px solid var(--border);
}

/* ── Discovery overlay ── */
.overlay {
  position: fixed; inset: 0;
  background: rgba(7,10,14,.88);
  backdrop-filter: blur(3px);
  display: flex; align-items: center; justify-content: center;
  z-index: 50;
}
.overlay.hidden { display: none; }
.card {
  background: var(--surface2);
  border: 1px solid var(--border);
  padding: 24px 28px;
  width: 420px;
  display: flex; flex-direction: column; gap: 14px;
}
.card-title { font-size: 13px; letter-spacing: 2px; text-transform: uppercase; color: var(--green-hi); }
.inp {
  background: var(--bg); border: 1px solid var(--border);
  color: var(--text); font-family: var(--mono); font-size: 13px;
  padding: 6px 10px; width: 100%;
}
.inp:focus { outline: none; border-color: var(--blue); }
.inp-row { display: flex; gap: 8px; }
.srv-list { display: flex; flex-direction: column; gap: 5px; }
.srv-item {
  padding: 7px 12px; border: 1px solid var(--border);
  cursor: pointer; color: var(--blue); transition: border-color .12s, background .12s;
}
.srv-item:hover { border-color: var(--blue); background: rgba(88,166,255,.06); }
.sep { border-top: 1px solid var(--border); }
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <div>
    <div class="hdr-logo">NODE v1</div>
    <div class="hdr-sub">ALPACA DASHBOARD</div>
  </div>

  <div class="conn-pill">
    <span class="dot dot-gray" id="connDot"></span>
    <span id="connLabel">Disconnected</span>
  </div>

  <div class="hdr-server hidden" id="hdrServer">
    Server: <span id="hdrAddr"></span>
  </div>

  <div class="conn-pill" id="safetyPill" style="display:none">
    <span class="dot dot-green" id="safetyDot"></span>
    <span id="safetyLabel" style="letter-spacing:1px;font-size:11px;">SAFE</span>
    <span id="safetyReason" style="color:var(--dim);font-size:10px;margin-left:4px;"></span>
  </div>

  <div style="color:var(--dim);font-size:11px;" id="sunEl"></div>

  <div class="hdr-right">
    <button class="btn btn-blue"  onclick="showDiscover()">Discover</button>
    <button class="btn btn-green" id="btnRun"   onclick="apiRun()"   disabled>Run Sequence</button>
    <button class="btn btn-red"   id="btnAbort" onclick="apiAbort()" disabled>Abort</button>
  </div>
</div>

<!-- Main grid -->
<div class="main">

  <!-- Sequence strip -->
  <div class="seq-panel">
    <div class="panel-label">Sequence Progress</div>
    <div class="steps" id="steps"></div>
    <div class="hold-bar hidden" id="holdBar"></div>
    <div class="err-text hidden"  id="errText"></div>
  </div>

  <!-- Telescope -->
  <div class="panel">
    <div class="panel-hdr">
      <div class="panel-name">
        <span class="dot dot-gray" id="telDot"></span>
        Telescope
      </div>
      <div class="badges" id="telBadges"></div>
    </div>
    <div class="coords">
      <div class="coord-lbl">R.A.</div>
      <div class="coord-val dim" id="telRA">—</div>
      <div class="coord-lbl">Dec</div>
      <div class="coord-val dim" id="telDec">—</div>
    </div>
    <div class="coord-raw" id="telRaw"></div>
    <div style="margin-top:10px;border-top:1px solid var(--border);padding-top:10px;">
      <div class="panel-label" style="margin-bottom:6px;">Slew Target</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
        <div>
          <div style="font-size:10px;color:var(--dim);letter-spacing:1px;margin-bottom:3px;">R.A. (decimal hours)</div>
          <input class="inp" id="slewRA" type="number" min="0" max="23.9999" step="0.0001" placeholder="0.0000">
        </div>
        <div>
          <div style="font-size:10px;color:var(--dim);letter-spacing:1px;margin-bottom:3px;">Dec (decimal degrees)</div>
          <input class="inp" id="slewDec" type="number" min="-90" max="90" step="0.0001" placeholder="0.0000">
        </div>
      </div>
      <button class="btn btn-blue" id="btnSlew" onclick="apiSlew()" style="margin-top:8px;width:100%;" disabled>Slew to Target</button>
    </div>
  </div>

  <!-- Camera -->
  <div class="panel">
    <div class="panel-hdr">
      <div class="panel-name">
        <span class="dot dot-gray" id="camDot"></span>
        Camera
      </div>
      <div id="camReady" style="font-size:11px;color:var(--gray)"></div>
    </div>
    <div class="cam-state cs-idle" id="camState">—</div>
    <div class="cam-sub" id="camSub"></div>
  </div>

  <!-- Image panel (hidden until an exposure is captured) -->
  <div class="img-panel hidden" id="imgPanel">
    <div class="panel-label">Last Exposure</div>
    <div class="img-inner">
      <div class="img-frame">
        <img id="lastImg" src="" alt="Last exposure">
      </div>
      <div class="img-meta" id="imgMeta"></div>
    </div>
  </div>


</div><!-- /main -->

<!-- Log footer (pinned to bottom) -->
<div class="log-footer">
  <div class="log-panel">
    <div class="log-hdr">
      <div class="panel-label" style="margin:0">Live Log</div>
      <div style="display:flex;gap:8px;align-items:center;">
        <span class="count-badge" id="logCount">0 lines</span>
        <button class="btn btn-dim" style="padding:2px 8px;font-size:10px;" onclick="clearLog()">Clear</button>
      </div>
    </div>
    <div class="log-body" id="logBody"></div>
  </div>
</div>

<!-- Discovery overlay -->
<div class="overlay hidden" id="overlay">
  <div class="card">
    <div class="card-title">Connect to ALPACA Server</div>

    <button class="btn btn-blue" id="scanBtn" onclick="doScan()" style="width:100%">
      Scan LAN for servers
    </button>
    <div class="srv-list" id="srvList"></div>

    <div class="sep"></div>
    <div style="color:var(--dim);font-size:11px;">Manual entry</div>
    <div class="inp-row">
      <input class="inp" id="mHost" placeholder="192.168.1.x" style="flex:1">
      <input class="inp" id="mPort" placeholder="11111" style="width:80px">
    </div>
    <div style="display:flex;gap:8px;">
      <button class="btn btn-green" onclick="doManualConnect()" style="flex:1">Connect</button>
      <button class="btn btn-dim" onclick="hideDiscover()">Cancel</button>
    </div>
  </div>
</div>

<script>
// ── Step definitions ────────────────────────────────────────────────────────

const STEPS = [
  { id: "discovering",      label: "Discover" },
  { id: "connecting",       label: "Connect" },
  { id: "unpark",           label: "Unpark" },
  { id: "tracking_on",      label: "Tracking" },
  { id: "verify_movement",  label: "Verify" },
  { id: "slew",             label: "Slew" },
  { id: "hold",             label: "Hold" },
  { id: "expose",           label: "Expose" },
  { id: "park",             label: "Park" },
  { id: "done",             label: "Done" },
];

const STEP_ORDER = STEPS.map(s => s.id);

// Build step chips
(function buildSteps() {
  const c = document.getElementById("steps");
  STEPS.forEach((s, i) => {
    if (i > 0) {
      const a = document.createElement("span");
      a.className = "step-arrow"; a.textContent = "›"; c.appendChild(a);
    }
    const chip = document.createElement("div");
    chip.className = "step-chip"; chip.id = "chip-" + s.id;
    chip.textContent = s.label; c.appendChild(chip);
  });
})();


// ── Status polling ──────────────────────────────────────────────────────────

async function poll() {
  try {
    const r = await fetch("/api/status");
    render(await r.json());
  } catch {}
}
setInterval(poll, 1000);
poll();

function render(s) {
  renderHeader(s);
  renderSteps(s);
  renderTelescope(s.telescope || {});
  renderCamera(s.camera || {});
  renderSafety(s.safety || {});
  renderImage(s);
}

let _imageFetched = false;

async function renderImage(s) {
  if (!s.image_captured) return;
  if (_imageFetched) return;
  _imageFetched = true;

  const panel   = document.getElementById("imgPanel");
  const img     = document.getElementById("lastImg");
  const meta    = document.getElementById("imgMeta");

  panel.classList.remove("hidden");
  meta.innerHTML = "Downloading…";

  try {
    const r = await fetch("/api/image");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    img.src = url;

    const kb = (blob.size / 1024).toFixed(1);
    const ts = new Date().toLocaleTimeString();
    img.onload = () => {
      meta.innerHTML =
        `Captured: <span>${ts}</span><br>` +
        `Size: <span>${img.naturalWidth} × ${img.naturalHeight} px</span><br>` +
        `File: <span>${kb} KB (PNG)</span>`;
    };
  } catch (e) {
    meta.innerHTML = `<span style="color:var(--red)">Image load failed: ${e.message}</span>`;
    _imageFetched = false;
  }
}

function renderSafety(sf) {
  const pill   = document.getElementById("safetyPill");
  const dot    = document.getElementById("safetyDot");
  const label  = document.getElementById("safetyLabel");
  const reason = document.getElementById("safetyReason");
  const sunEl  = document.getElementById("sunEl");

  if (!sf || sf.safe === undefined) { pill.style.display = "none"; return; }
  pill.style.display = "flex";

  if (sf.safe) {
    dot.className    = "dot dot-green";
    label.textContent = "SAFE";
    label.style.color = "var(--green)";
    reason.textContent = sf.heartbeat_ok ? "" : "hb?";
  } else {
    dot.className     = "dot dot-red pulse";
    label.textContent = "UNSAFE";
    label.style.color = "var(--red)";
    reason.textContent = sf.reason ? `· ${sf.reason}` : "";
  }

  if (sf.sun_elevation !== null && sf.sun_elevation !== undefined) {
    const el  = sf.sun_elevation.toFixed(1);
    const thr = sf.dawn_threshold !== undefined ? sf.dawn_threshold.toFixed(0) : "-18";
    const col = sf.sun_elevation > sf.dawn_threshold
      ? "var(--yellow)" : "var(--dim)";
    sunEl.style.color = col;
    sunEl.textContent = `☀ ${el >= 0 ? "+" : ""}${el}°`;
    sunEl.title       = `Sun elevation (dawn at ${thr}°)`;
  } else {
    sunEl.textContent = "";
  }
}

function renderHeader(s) {
  const dot   = document.getElementById("connDot");
  const label = document.getElementById("connLabel");
  const phase = s.phase || "idle";

  // Server address
  if (s.server) {
    document.getElementById("hdrServer").classList.remove("hidden");
    document.getElementById("hdrAddr").textContent =
      `${s.server.address}:${s.server.port}`;
  }

  if (phase === "idle") {
    dot.className = "dot dot-gray"; label.textContent = "Idle";
  } else if (phase === "error") {
    dot.className = "dot dot-red";  label.textContent = "Error";
  } else if (phase === "done") {
    dot.className = "dot dot-green"; label.textContent = "Complete";
  } else if (s.connected) {
    dot.className = "dot dot-green pulse"; label.textContent = "Connected";
  } else {
    dot.className = "dot dot-yellow pulse";
    label.textContent = phase.replace("_", " ") + "…";
  }

  document.getElementById("btnRun").disabled   = s.run_active;
  document.getElementById("btnAbort").disabled = !s.run_active;
}

function renderSteps(s) {
  const phase    = s.phase || "idle";
  const phaseIdx = STEP_ORDER.indexOf(phase);

  STEPS.forEach((step, i) => {
    const chip = document.getElementById("chip-" + step.id);
    chip.className = "step-chip";
    if (phase === step.id && phase !== "idle") {
      chip.classList.add("active");
      if (phase !== "done") chip.classList.add("pulse");
    } else if (phase === "error" && i === phaseIdx) {
      chip.classList.add("error");
    } else if (phaseIdx > i || phase === "done") {
      chip.classList.add("done");
    }
  });

  // Hold countdown
  const holdBar = document.getElementById("holdBar");
  if (s.hold_remaining !== null && s.hold_remaining !== undefined) {
    holdBar.classList.remove("hidden");
    const m  = Math.floor(s.hold_remaining / 60);
    const sc = String(s.hold_remaining % 60).padStart(2, "0");
    holdBar.textContent = `⏱  HOLD  ${m}:${sc}  remaining`;
  } else {
    holdBar.classList.add("hidden");
  }

  // Error message
  const errText = document.getElementById("errText");
  if (s.error) {
    errText.classList.remove("hidden");
    errText.textContent = "ERROR: " + s.error;
  } else {
    errText.classList.add("hidden");
  }
}

function fmtRA(h) {
  if (h == null) return "—";
  const hr  = Math.floor(h);
  const mn  = Math.floor((h - hr) * 60);
  const sec = ((h - hr) * 3600 - mn * 60).toFixed(1);
  return `${pad(hr)}h ${pad(mn)}m ${String(sec).padStart(4,"0")}s`;
}

function fmtDec(d) {
  if (d == null) return "—";
  const sign = d >= 0 ? "+" : "−";
  const abs = Math.abs(d);
  const deg = Math.floor(abs);
  const mn  = Math.floor((abs - deg) * 60);
  const sec = ((abs - deg) * 3600 - mn * 60).toFixed(1);
  return `${sign}${pad(deg)}° ${pad(mn)}' ${String(sec).padStart(4,"0")}"`;
}

function pad(n) { return String(n).padStart(2, "0"); }

function renderTelescope(t) {
  document.getElementById("telDot").className =
    t.connected ? "dot dot-green" : "dot dot-gray";

  const raEl  = document.getElementById("telRA");
  const decEl = document.getElementById("telDec");
  const rawEl = document.getElementById("telRaw");

  if (t.connected && t.ra != null) {
    raEl.textContent  = fmtRA(t.ra);
    decEl.textContent = fmtDec(t.dec);
    raEl.className    = "coord-val";
    decEl.className   = "coord-val";
    rawEl.textContent = `RA ${t.ra.toFixed(4)} h  ·  Dec ${t.dec?.toFixed(4)} °`;
  } else {
    raEl.textContent  = "—"; raEl.className  = "coord-val dim";
    decEl.textContent = "—"; decEl.className = "coord-val dim";
    rawEl.textContent = "";
  }

  const slewBtn = document.getElementById("btnSlew");
  if (slewBtn) slewBtn.disabled = !t.connected || t.slewing;
  const b = document.getElementById("telBadges");
  b.innerHTML = "";
  if (t.connected) {
    if (t.slewing)   b.innerHTML += `<span class="badge badge-warn pulse">Slewing</span>`;
    if (t.tracking)  b.innerHTML += `<span class="badge badge-on">Tracking</span>`;
    if (t.parked)    b.innerHTML += `<span class="badge badge-warn">Parked</span>`;
    if (!t.slewing && !t.tracking && !t.parked)
      b.innerHTML += `<span class="badge">Idle</span>`;
  } else if (t.enabled) {
    b.innerHTML += `<span class="badge badge-err">Disconnected</span>`;
  }
}

const CAM_CLASSES = ["cs-idle","cs-wait","cs-expose","cs-read","cs-dl","cs-error"];

function renderCamera(c) {
  document.getElementById("camDot").className =
    c.connected ? "dot dot-green" : "dot dot-gray";

  const stEl  = document.getElementById("camState");
  const subEl = document.getElementById("camSub");
  const rdEl  = document.getElementById("camReady");

  if (c.connected) {
    stEl.textContent = (c.state_name || "—").toUpperCase();
    stEl.className   = "cam-state " + (CAM_CLASSES[c.state] || "cs-idle");
    if (c.state === 2) stEl.classList.add("pulse");
    subEl.textContent = `ALPACA state ${c.state}`;
    rdEl.textContent  = c.image_ready ? "✓ IMAGE READY" : "";
    rdEl.style.color  = c.image_ready ? "var(--green)" : "var(--gray)";
  } else {
    stEl.textContent = "—"; stEl.className = "cam-state cs-idle";
    subEl.textContent = c.enabled ? "Disconnected" : "Not enabled";
    rdEl.textContent  = "";
  }
}


// ── Log stream (SSE) ────────────────────────────────────────────────────────

let logCount  = 0;
let autoScroll = true;
const logBody = document.getElementById("logBody");

logBody.addEventListener("scroll", () => {
  autoScroll = logBody.scrollTop + logBody.clientHeight >= logBody.scrollHeight - 24;
});

function appendLog(entry) {
  logCount++;
  document.getElementById("logCount").textContent = logCount + " lines";

  // Parse name from formatted string: "HH:MM:SS [LEVEL] name: message"
  const raw    = entry.msg || "";
  const match  = raw.match(/^\S+\s+\[\w+\]\s+([^:]+):\s(.*)/s);
  const name   = match ? match[1] : (entry.name || "");
  const msg    = match ? match[2] : raw;

  const line = document.createElement("div");
  line.className = "ll";

  const t = document.createElement("span");
  t.className = "lt"; t.textContent = entry.time || "";

  const lv = document.createElement("span");
  lv.className = "llv " + entry.level;
  lv.textContent = "[" + (entry.level || "").substring(0, 4) + "]";

  const nm = document.createElement("span");
  nm.className = "ln"; nm.textContent = name;

  const ms = document.createElement("span");
  ms.className = "lm";
  if (entry.level === "WARNING") ms.classList.add("warn-msg");
  if (entry.level === "ERROR")   ms.classList.add("err-msg");
  ms.textContent = msg;

  line.appendChild(t); line.appendChild(lv);
  line.appendChild(nm); line.appendChild(ms);
  logBody.appendChild(line);

  if (autoScroll) logBody.scrollTop = logBody.scrollHeight;
}

function clearLog() {
  logBody.innerHTML = "";
  logCount = 0;
  document.getElementById("logCount").textContent = "0 lines";
}

const es = new EventSource("/api/logs");
es.onmessage = e => { try { appendLog(JSON.parse(e.data)); } catch {} };


// ── Discovery overlay ───────────────────────────────────────────────────────

function showDiscover()  { document.getElementById("overlay").classList.remove("hidden"); }
function hideDiscover()  { document.getElementById("overlay").classList.add("hidden"); }

async function doScan() {
  const btn = document.getElementById("scanBtn");
  btn.textContent = "Scanning…"; btn.disabled = true;
  document.getElementById("srvList").innerHTML = "";
  try {
    const r   = await fetch("/api/discover", { method: "POST" });
    const data = await r.json();
    const list = document.getElementById("srvList");
    if (data.servers?.length) {
      data.servers.forEach(srv => {
        const item = document.createElement("div");
        item.className = "srv-item";
        item.textContent = `${srv.address}:${srv.port}`;
        item.onclick = () => connectTo(srv.address, srv.port);
        list.appendChild(item);
      });
    } else {
      list.innerHTML = '<div style="color:var(--dim);font-size:12px;">No servers found on LAN.</div>';
    }
  } catch {
    document.getElementById("srvList").innerHTML =
      '<div style="color:var(--red);font-size:12px;">Discovery request failed.</div>';
  }
  btn.textContent = "Scan LAN for servers"; btn.disabled = false;
}

async function doManualConnect() {
  const host = document.getElementById("mHost").value.trim();
  const port = parseInt(document.getElementById("mPort").value.trim() || "11111");
  if (!host) return;
  await connectTo(host, port);
}

async function connectTo(host, port) {
  hideDiscover();
  try {
    const r = await fetch("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, port }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "failed");
    document.getElementById("btnRun").disabled = false;
  } catch (e) {
    alert("Connection failed: " + e.message);
  }
}

async function apiRun() {
  try {
    const r = await fetch("/api/run", { method: "POST" });
    const d = await r.json();
    if (!d.ok) { alert(d.error || "Run failed"); return; }
    // Reset image panel for the new run
    _imageFetched = false;
    document.getElementById("imgPanel").classList.add("hidden");
    document.getElementById("lastImg").src = "";
    document.getElementById("imgMeta").innerHTML = "";
  } catch (e) {
    alert("Run failed: " + e.message);
  }
}

async function apiAbort() {
  await fetch("/api/abort", { method: "POST" });
}

async function apiSlew() {
  const ra  = parseFloat(document.getElementById("slewRA").value);
  const dec = parseFloat(document.getElementById("slewDec").value);
  if (isNaN(ra) || isNaN(dec)) { alert("Enter valid RA (h) and Dec (°) values."); return; }
  const btn = document.getElementById("btnSlew");
  btn.disabled = true; btn.textContent = "Slewing…";
  try {
    const r = await fetch("/api/slew", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ra, dec }),
    });
    const d = await r.json();
    if (!d.ok) alert(d.error || "Slew failed");
  } catch (e) {
    alert("Slew failed: " + e.message);
  }
  btn.textContent = "Slew to Target";
  // disabled state re-evaluated on next status poll
}
</script>
</body>
</html>"""


# ── Public entry point ─────────────────────────────────────────────────────────

def launch(port: int = 5173) -> None:
    """
    Start the Flask dashboard, open the browser, and immediately begin the
    sequence.  Blocks until the user hits Ctrl-C.

    Called by both ``python dashboard.py`` and ``python main.py``.
    """
    global _safety_mgr

    import urllib.request
    import webbrowser

    cfg = _load_config()
    log_cfg = cfg.get("logging", {})
    logging.basicConfig(
        level=log_cfg.get("level", "INFO"),
        format=log_cfg.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    )

    # Create and start the safety manager from the main thread so that
    # OS signal handlers (SIGTERM, SIGINT) are registered correctly.
    _safety_mgr = SafetyManager(config=cfg, on_unsafe=_on_safety_unsafe)
    _safety_mgr.start()

    # Start Flask in a daemon thread so it dies when the process exits.
    flask_thread = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0", port=port, debug=False,
            threaded=True, use_reloader=False,
        ),
        daemon=True,
        name="flask",
    )
    flask_thread.start()

    # Wait until the server is accepting connections (up to 5 s).
    url = f"http://localhost:{port}"
    for _ in range(20):
        try:
            urllib.request.urlopen(url, timeout=0.5)
            break
        except Exception:
            time.sleep(0.25)

    print(f"\n  NODE v1 Dashboard  →  {url}\n", file=sys.__stdout__)

    # Open the default browser.
    webbrowser.open(url)

    # Kick off the sequence automatically.
    _run_abort.clear()
    seq_thread = threading.Thread(
        target=_run_sequence, args=(cfg,), daemon=True, name="sequence"
    )
    seq_thread.start()

    # Block the main thread so the process stays alive.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down.", file=sys.__stdout__)
    finally:
        if _safety_mgr is not None:
            _safety_mgr.stop()


if __name__ == "__main__":
    launch()
