#!/usr/bin/env python3
"""
NODE v1 — Interactive ALPACA control panel.

Run:  python dashboard.py
Then open http://localhost:5173 in a browser.
"""

import base64
import copy
import io
import json
import logging
import os
import queue
import sys
import threading
import time
from typing import Any, Optional

import yaml
from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context

from alpaca.discovery import discover_servers
from alpaca.safety_manager import SafetyManager
from alpaca.telescope import Telescope
from alpaca.camera import Camera
from image_watcher import ImageWatcher
from photometry import run_pipeline as _run_photometry


app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ── Shared state ───────────────────────────────────────────────────────────────

_state: dict[str, Any] = {
    "server":    None,
    "connected": False,
    "telescope": {
        "enabled":   False,
        "connected": False,
        "slewing":   None,
        "parked":    None,
        "tracking":  None,
        "ra":        None,
        "dec":       None,
        "busy":      False,
    },
    "camera": {
        "enabled":     False,
        "connected":   False,
        "state":       None,
        "state_name":  None,
        "image_ready": None,
        "exposing":    False,
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
    "image_captured": False,
    "image_id":       0,
    "error":          None,
    "pier_cam": {
        "enabled":   False,
        "streaming": False,
        "error":     None,
    },
    "image_watcher": {
        "enabled":    False,
        "watch_path": "",
        "last_file":  None,
        "last_header": {},
    },
    "photometry": {
        "enabled":    False,
        "last_result": None,   # most recent measurement dict
        "running":    False,
    },
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
    with _subscribers_lock:
        _log_history.append(entry)
        if len(_log_history) > 300:
            del _log_history[:-300]
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


class _StdoutCapture:
    def write(self, text: str) -> None:
        text = text.strip()
        if text:
            logging.getLogger("stdout").info(text)

    def flush(self) -> None:
        pass


sys.stdout = _StdoutCapture()  # type: ignore[assignment]


# ── Device handles ─────────────────────────────────────────────────────────────

_tel: Optional[Telescope] = None
_cam: Optional[Camera] = None
_last_image_b64: Optional[str] = None
_last_image_lock = threading.Lock()

_pier_cam_frame: Optional[bytes] = None
_pier_cam_frame_lock = threading.Lock()
_pier_cam_pause = threading.Event()
_pier_cam_stop  = threading.Event()


def _capture_image() -> None:
    global _last_image_b64
    if _cam is None:
        return
    try:
        import numpy as np
        from PIL import Image

        logger.info("Downloading image array from camera…")
        raw = _cam.image_array()
        arr = np.array(raw, dtype=np.float32)

        if arr.ndim == 3 and arr.shape[0] in (1, 3):
            arr = np.transpose(arr, (1, 2, 0))
            if arr.shape[2] == 1:
                arr = arr[:, :, 0]

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
            _state["image_id"] += 1
        logger.info("Image stored — %.1f KB PNG", len(b64) * 3 / 4 / 1024)
    except Exception as exc:
        logger.error("Image capture failed: %s", exc)


# ── Image watcher ──────────────────────────────────────────────────────────────

_image_watcher: Optional[ImageWatcher] = None


def _fits_to_png_b64(path: str) -> Optional[str]:
    try:
        from astropy.io import fits
        import numpy as np
        from PIL import Image

        with fits.open(path, memmap=False, ignore_missing_simple=True) as hdul:
            data = hdul[0].data

        if data is None:
            return None

        arr = np.array(data, dtype=np.float32)

        # Handle 3-D cubes (C, H, W) → (H, W) by taking the first plane
        if arr.ndim == 3:
            arr = arr[0]

        mn, mx = float(arr.min()), float(arr.max())
        if mx > mn:
            arr = (arr - mn) / (mx - mn) * 255.0
        arr = arr.clip(0, 255).astype(np.uint8)

        img = Image.fromarray(arr, mode="L")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    except Exception as exc:
        logger.error("FITS→PNG conversion failed: %s", exc)
        return None


def _on_new_fits(info: dict) -> None:
    path   = info["path"]
    header = info.get("header", {})
    kb     = info.get("size_kb", 0.0)

    obj     = header.get("OBJECT", "")
    exptime = header.get("EXPTIME") or header.get("EXPOSURE")
    filter_ = header.get("FILTER", "")

    parts = [f"{kb:.1f} KB"]
    if obj:     parts.append(f"obj={obj}")
    if exptime: parts.append(f"exp={exptime}s")
    if filter_: parts.append(f"filter={filter_}")
    logger.info("FITS captured: %s  (%s)", os.path.basename(path), "  ".join(parts))

    b64 = _fits_to_png_b64(path)
    if b64:
        with _last_image_lock:
            global _last_image_b64
            _last_image_b64 = b64
        with _state_lock:
            _state["image_captured"] = True
            _state["image_id"]      += 1

    with _state_lock:
        _state["image_watcher"]["last_file"]  = os.path.basename(path)
        _state["image_watcher"]["last_header"] = header

    # Optionally run photometry pipeline in background thread
    with _state_lock:
        phot_enabled = _state["photometry"]["enabled"]
        phot_running = _state["photometry"]["running"]

    if phot_enabled and not phot_running:
        threading.Thread(
            target=_run_photometry_bg,
            args=(path,),
            daemon=True,
            name="photometry",
        ).start()


def _run_photometry_bg(fits_path: str) -> None:
    """Run the photometry pipeline in a background thread and store the result."""
    with _state_lock:
        _state["photometry"]["running"] = True
    try:
        cfg = _load_config()
        result = _run_photometry(fits_path, cfg)
        with _state_lock:
            _state["photometry"]["last_result"] = result
        if result:
            logger.info(
                "Photometry: %s  mag=%.3f±%.3f  SNR=%.1f  quality=%s",
                result["target_name"], result["magnitude"],
                result["uncertainty"], result["snr"], result["quality_flag"],
            )
        else:
            logger.warning("Photometry pipeline returned no result for %s",
                           os.path.basename(fits_path))
    except Exception as exc:
        logger.error("Photometry pipeline crashed: %s", exc)
    finally:
        with _state_lock:
            _state["photometry"]["running"] = False


# ── Safety manager ─────────────────────────────────────────────────────────────

_safety_mgr: Optional[SafetyManager] = None


def _on_safety_unsafe() -> None:
    reason = _safety_mgr.status()["reason"] if _safety_mgr else "unknown"
    with _state_lock:
        _state["error"] = f"Safety stop: {reason}"
    logger.critical("Safety manager triggered: %s", reason)


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


# ── Config helper ──────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        with open("config.yaml") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        return {}


# ── Pier cam (ZWO SDK live preview) ───────────────────────────────────────────

def _pier_cam_loop() -> None:
    global _pier_cam_frame

    cfg = _load_config()
    pc  = cfg.get("pier_cam", {})

    device_index  = int(pc.get("device_index", 0))
    exposure_us   = int(float(pc.get("exposure_ms", 80)) * 1000)
    gain          = int(pc.get("gain", 200))
    bin_size      = int(pc.get("bin", 2))
    jpeg_quality  = int(pc.get("jpeg_quality", 75))
    target_fps    = float(pc.get("target_fps", 10))
    sdk_lib       = str(pc.get("sdk_lib", "") or "")

    try:
        import zwoasi as asi
    except ImportError:
        logger.error("Pier cam: zwoasi not installed — run: pip install zwoasi")
        with _state_lock:
            _state["pier_cam"]["error"] = "zwoasi not installed"
        return

    if sdk_lib:
        try:
            asi.init(sdk_lib)
        except Exception as exc:
            logger.error("Pier cam: SDK init failed: %s", exc)
            with _state_lock:
                _state["pier_cam"]["error"] = f"SDK init: {exc}"
            return

    cam = None
    while not _pier_cam_stop.is_set():
        try:
            num = asi.get_num_cameras()
            if num == 0:
                raise RuntimeError("No ASI cameras detected")
            if device_index >= num:
                raise RuntimeError(f"device_index {device_index} >= cameras found ({num})")

            cam  = asi.Camera(device_index)
            info = cam.get_camera_property()
            logger.info("Pier cam: %s  (%dx%d)", info["Name"],
                        info["MaxWidth"], info["MaxHeight"])

            cam.set_control_value(asi.ASI_BANDWIDTHOVERLOAD, 80)
            cam.set_control_value(asi.ASI_GAIN, gain)
            cam.set_control_value(asi.ASI_EXPOSURE, exposure_us)

            w        = (info["MaxWidth"]  // bin_size) & ~3
            h        = (info["MaxHeight"] // bin_size) & ~1
            is_color = bool(info.get("IsColorCam", False))
            img_type = asi.ASI_IMG_RGB24 if is_color else asi.ASI_IMG_Y8
            cam.set_roi(width=w, height=h, bins=bin_size, image_type=img_type)
            cam.start_video_capture()

            with _state_lock:
                _state["pier_cam"].update(streaming=True, error=None)

            frame_interval = 1.0 / max(1.0, target_fps)
            next_frame     = time.monotonic()

            while not _pier_cam_stop.is_set():
                if _pier_cam_pause.is_set():
                    time.sleep(0.05)
                    next_frame = time.monotonic() + frame_interval
                    continue

                now = time.monotonic()
                if now < next_frame:
                    time.sleep(next_frame - now)
                    continue
                next_frame = time.monotonic() + frame_interval

                data = cam.capture_video_frame(timeout=int(exposure_us / 1000 + 2000))
                from PIL import Image as _PILImage
                mode = "RGB" if is_color else "L"
                img  = _PILImage.fromarray(data, mode)
                buf  = io.BytesIO()
                img.save(buf, format="JPEG", quality=jpeg_quality)
                with _pier_cam_frame_lock:
                    _pier_cam_frame = buf.getvalue()

        except Exception as exc:
            if _pier_cam_stop.is_set():
                break
            logger.warning("Pier cam: %s — retry in 5 s", exc)
            with _state_lock:
                _state["pier_cam"].update(streaming=False, error=str(exc))
            try:
                if cam is not None:
                    cam.stop_video_capture()
                    cam.close()
                    cam = None
            except Exception:
                pass
            time.sleep(5)

    with _state_lock:
        _state["pier_cam"]["streaming"] = False
    try:
        if cam is not None:
            cam.stop_video_capture()
            cam.close()
    except Exception:
        pass
    logger.info("Pier cam stopped")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(_HTML)


@app.route("/api/status")
def api_status():
    with _state_lock:
        snapshot = copy.deepcopy(_state)
    return jsonify(snapshot)


@app.route("/api/logs")
def api_logs():
    q: queue.Queue = queue.Queue(maxsize=400)
    with _subscribers_lock:
        history_snapshot = list(_log_history)
        _subscribers.append(q)
    for entry in history_snapshot:
        try:
            q.put_nowait(entry)
        except queue.Full:
            break

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


_SEESTAR_AP_IP = "192.168.4.1"


@app.route("/api/connect", methods=["POST"])
def api_connect():
    global _tel, _cam
    data    = request.get_json(force=True) or {}
    host    = data.get("host", "")
    port    = int(data.get("port", 11111))

    if host == _SEESTAR_AP_IP:
        return jsonify({
            "error": (
                "Seestar is in Access Point (hotspot) mode — ALPACA is not active. "
                "Connect the Seestar to your home Wi-Fi via Station Mode in the Seestar "
                "App, then reconnect your computer to the same network and try again."
            )
        }), 400

    cfg     = _load_config()
    api_ver = cfg.get("alpaca", {}).get("api_version", 1)
    devices = cfg.get("devices", {})

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


@app.route("/api/telescope/unpark", methods=["POST"])
def api_unpark():
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400

    def _do():
        with _state_lock:
            _state["telescope"]["busy"] = True
        try:
            _tel.unpark()
        except Exception as exc:
            logger.error("Unpark failed: %s", exc)
        finally:
            with _state_lock:
                _state["telescope"]["busy"] = False

    threading.Thread(target=_do, daemon=True, name="tel-unpark").start()
    logger.info("Unpark commanded")
    return jsonify({"ok": True})


@app.route("/api/telescope/park", methods=["POST"])
def api_park():
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400

    def _do():
        with _state_lock:
            _state["telescope"]["busy"] = True
        try:
            _tel.park()
        except Exception as exc:
            logger.error("Park failed: %s", exc)
        finally:
            with _state_lock:
                _state["telescope"]["busy"] = False

    threading.Thread(target=_do, daemon=True, name="tel-park").start()
    logger.info("Park commanded")
    return jsonify({"ok": True})


@app.route("/api/telescope/tracking", methods=["POST"])
def api_tracking():
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400
    data    = request.get_json(force=True) or {}
    enabled = bool(data.get("enabled", True))
    try:
        _tel.set_tracking(enabled)
    except Exception as exc:
        logger.error("Set tracking failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/slew", methods=["POST"])
def api_slew():
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

    def _do():
        try:
            _tel.slew_to_coordinates(ra=ra, dec=dec)
        except Exception as exc:
            logger.error("Slew failed: %s", exc)
            with _state_lock:
                _state["error"] = str(exc)

    threading.Thread(target=_do, daemon=True, name="tel-slew").start()
    logger.info("Slew commanded: RA=%.4f h  Dec=%.4f °", ra, dec)
    return jsonify({"ok": True})


@app.route("/api/telescope/nudge", methods=["POST"])
def api_nudge():
    import math
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400
    data = request.get_json(force=True) or {}
    direction = data.get("direction", "").upper()
    if direction not in ("N", "S", "E", "W"):
        return jsonify({"error": "direction must be N/S/E/W"}), 400
    try:
        step_arcsec = float(data.get("step", 60))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid step"}), 400
    if not (1 <= step_arcsec <= 3600):
        return jsonify({"error": "step must be 1–3600 arcsec"}), 400

    try:
        cur_ra  = _tel.ra()
        cur_dec = _tel.dec()
    except Exception as exc:
        return jsonify({"error": f"Could not read position: {exc}"}), 500

    step_deg = step_arcsec / 3600.0
    cos_dec  = math.cos(math.radians(cur_dec)) or 1e-9

    if direction == "N":
        new_ra, new_dec = cur_ra, min(90.0, cur_dec + step_deg)
    elif direction == "S":
        new_ra, new_dec = cur_ra, max(-90.0, cur_dec - step_deg)
    elif direction == "E":
        ra_delta = step_deg / (15.0 * cos_dec)
        new_ra   = (cur_ra - ra_delta) % 24.0
        new_dec  = cur_dec
    else:  # W
        ra_delta = step_deg / (15.0 * cos_dec)
        new_ra   = (cur_ra + ra_delta) % 24.0
        new_dec  = cur_dec

    def _do():
        try:
            _tel.slew_to_coordinates(ra=new_ra, dec=new_dec)
        except Exception as exc:
            logger.error("Nudge slew failed: %s", exc)

    threading.Thread(target=_do, daemon=True, name="tel-nudge").start()
    logger.info("Nudge %s %.0f\" → RA=%.4f h  Dec=%.4f °", direction, step_arcsec, new_ra, new_dec)
    return jsonify({"ok": True})


@app.route("/api/camera/expose", methods=["POST"])
def api_expose():
    if _cam is None:
        return jsonify({"error": "Camera not connected"}), 400
    with _state_lock:
        if _state["camera"]["exposing"]:
            return jsonify({"error": "Exposure already in progress"}), 409

    data     = request.get_json(force=True) or {}
    duration = float(data.get("duration", 1.0))
    binning  = int(data.get("binning", 1))

    if duration <= 0:
        return jsonify({"error": "Duration must be > 0"}), 400
    if binning < 1:
        return jsonify({"error": "Binning must be >= 1"}), 400

    def _do():
        with _state_lock:
            _state["camera"]["exposing"]  = True
            _state["image_captured"]      = False
        _pier_cam_pause.set()
        time.sleep(0.15)
        try:
            _cam.set_binning(binning)
            _cam.expose(duration=duration, light=True)
            _capture_image()
        except Exception as exc:
            logger.error("Exposure failed: %s", exc)
        finally:
            with _state_lock:
                _state["camera"]["exposing"] = False
            _pier_cam_pause.clear()

    threading.Thread(target=_do, daemon=True, name="cam-expose").start()
    logger.info("Exposure started: %.2f s  binning %dx%d", duration, binning, binning)
    return jsonify({"ok": True})


@app.route("/api/camera/abort", methods=["POST"])
def api_abort_exposure():
    if _cam is None:
        return jsonify({"error": "Camera not connected"}), 400
    try:
        _cam.abort_exposure()
        with _state_lock:
            _state["camera"]["exposing"] = False
    except Exception as exc:
        logger.error("Abort exposure failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
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


@app.route("/api/pier-cam/stream")
def pier_cam_stream():
    def generate():
        while not _pier_cam_stop.is_set():
            with _pier_cam_frame_lock:
                frame = _pier_cam_frame
            if frame:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + frame + b"\r\n")
            time.sleep(0.05)

    return Response(
        stream_with_context(generate()),
        content_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/photometry")
def api_photometry():
    with _state_lock:
        snap = {
            "enabled":     _state["photometry"]["enabled"],
            "running":     _state["photometry"]["running"],
            "last_result": _state["photometry"]["last_result"],
        }
    return jsonify(snap)


@app.route("/api/pier-cam/snapshot")
def pier_cam_snapshot():
    with _pier_cam_frame_lock:
        frame = _pier_cam_frame
    if frame is None:
        return jsonify({"error": "No frame available"}), 404
    return Response(frame, content_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ── HTML template ──────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NODE v1 — ALPACA Control</title>
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

html {
  scroll-behavior: smooth;
}

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
.hdr-logo { font-size: 17px; font-weight: bold; color: var(--green-hi); letter-spacing: 3px; }
.hdr-sub  { color: var(--dim); font-size: 10px; letter-spacing: 2px; margin-top: 2px; }
.hdr-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.hdr-server { color: var(--dim); font-size: 12px; }
.hdr-server span { color: var(--blue); }
.hdr-server.hidden { display: none; }

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
.btn-green  { border-color: var(--green);  color: var(--green); }
.btn-green:hover:not(:disabled)  { background: var(--green);  color: var(--bg); }
.btn-red    { border-color: var(--red);    color: var(--red); }
.btn-red:hover:not(:disabled)    { background: var(--red);    color: var(--bg); }
.btn-blue   { border-color: var(--blue);   color: var(--blue); }
.btn-blue:hover:not(:disabled)   { background: var(--blue);   color: var(--bg); }
.btn-yellow { border-color: var(--yellow); color: var(--yellow); }
.btn-yellow:hover:not(:disabled) { background: var(--yellow); color: var(--bg); }
.btn-dim    { border-color: var(--gray);   color: var(--dim); }
.btn-dim:hover:not(:disabled)    { background: var(--gray);   color: var(--text); }
.btn:disabled { opacity: 0.3; cursor: not-allowed; }
.btn-full { width: 100%; }

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

/* ── Layout ── */
.main {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--border);
  overflow: hidden;
  min-height: 0;
}

.main-empty {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--gray);
  font-size: 12px;
  letter-spacing: 2px;
  flex-direction: column;
  gap: 8px;
}

.log-footer {
  flex-shrink: 0;
  height: 180px;
  background: var(--surface);
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Panels ── */
.panel {
  background: var(--surface);
  padding: 16px 20px;
  display: flex; flex-direction: column; gap: 12px;
  overflow-y: auto; min-height: 0;
}

.panel::-webkit-scrollbar {
  width: 6px;
}

.panel::-webkit-scrollbar-track {
  background: var(--bg);
}

.panel::-webkit-scrollbar-thumb {
  background: var(--border);
  border-radius: 3px;
}

.panel::-webkit-scrollbar-thumb:hover {
  background: var(--gray);
}

.img-col {
  grid-column: 1 / -1;
  background: var(--surface);
  display: flex; flex-direction: row;
  overflow: hidden; min-height: 0;
}
.img-col.hidden { display: none; }

.img-sub {
  background: var(--surface);
  padding: 14px 20px;
  display: flex; flex-direction: column; gap: 10px;
  overflow: hidden; min-height: 0;
}
.img-sub.hidden { display: none; }
.img-sub::-webkit-scrollbar { width: 6px; }
.img-sub::-webkit-scrollbar-track { background: var(--bg); }
.img-sub::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.img-sub::-webkit-scrollbar-thumb:hover { background: var(--gray); }

/* ── Pier cam ── */
.pier-cam-wrap {
  background: #000;
  display: flex; align-items: center; justify-content: center;
  flex: 1; min-height: 0; overflow: hidden;
}
.pier-cam-wrap img {
  display: block; max-width: 100%; max-height: 100%; width: 100%; height: 100%;
  object-fit: contain; image-rendering: auto;
}
.pier-cam-badge {
  font-size: 10px; letter-spacing: 1px; color: var(--dim); flex-shrink: 0;
}

.panel-hdr {
  display: flex; align-items: center; justify-content: space-between;
  border-bottom: 1px solid var(--border); padding-bottom: 10px;
  flex-shrink: 0;
}
.panel-name {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; font-weight: bold; letter-spacing: 2px;
  text-transform: uppercase;
}
.panel-label {
  font-size: 10px; letter-spacing: 2px; color: var(--dim);
  text-transform: uppercase;
}

.badges { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
.badge {
  padding: 2px 7px; font-size: 10px; letter-spacing: 1px;
  text-transform: uppercase; border: 1px solid var(--gray); color: var(--gray);
}
.badge-on   { border-color: var(--green);  color: var(--green); }
.badge-warn { border-color: var(--yellow); color: var(--yellow); }
.badge-err  { border-color: var(--red);    color: var(--red); }

/* ── Coordinates ── */
.coords { display: grid; grid-template-columns: 40px 1fr; gap: 4px 10px; align-items: center; }
.coord-lbl { color: var(--dim); font-size: 11px; text-align: right; }
.coord-val { font-size: 20px; color: var(--green-hi); letter-spacing: 2px; }
.coord-val.dim { color: var(--gray); }
.coord-raw { color: var(--dim); font-size: 11px; }

/* ── Control groups ── */
.ctrl-group {
  display: flex; flex-direction: column; gap: 6px;
}
.ctrl-row {
  display: flex; gap: 6px;
}
.ctrl-row .btn { flex: 1; }

.section-div {
  border-top: 1px solid var(--border);
  padding-top: 12px;
}

/* ── Input fields ── */
.inp {
  background: var(--bg); border: 1px solid var(--border);
  color: var(--text); font-family: var(--mono); font-size: 13px;
  padding: 6px 10px; width: 100%;
}
.inp:focus { outline: none; border-color: var(--blue); }
.inp-label { font-size: 10px; color: var(--dim); letter-spacing: 1px; margin-bottom: 3px; }
.inp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.inp-group { display: flex; flex-direction: column; }

/* ── Camera state ── */
.cam-state { font-size: 24px; letter-spacing: 3px; }
.cs-idle   { color: var(--gray); }
.cs-wait   { color: var(--dim); }
.cs-expose { color: var(--yellow); }
.cs-read   { color: var(--blue); }
.cs-dl     { color: var(--blue); }
.cs-error  { color: var(--red); }
.cam-sub   { color: var(--dim); font-size: 11px; }

/* ── Image panel ── */
.img-inner {
  display: flex; align-items: flex-start; gap: 20px;
  overflow-y: auto; min-height: 0; flex: 1;
}

.img-inner::-webkit-scrollbar {
  width: 6px;
}

.img-inner::-webkit-scrollbar-track {
  background: var(--bg);
}

.img-inner::-webkit-scrollbar-thumb {
  background: var(--border);
  border-radius: 3px;
}

.img-inner::-webkit-scrollbar-thumb:hover {
  background: var(--gray);
}

.img-frame {
  border: 1px solid var(--border); background: #000;
  flex-shrink: 0; max-width: 420px;
}
.img-frame img {
  display: block; max-width: 420px; max-height: 260px;
  width: 100%; image-rendering: pixelated;
}
.img-meta { color: var(--dim); font-size: 11px; line-height: 1.9; }
.img-meta span { color: var(--text); }

/* ── Log ── */
.log-panel { display: flex; flex-direction: column; overflow: hidden; flex: 1; }
.log-hdr {
  padding: 6px 20px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
  flex-shrink: 0;
}
.log-body {
  flex: 1; overflow-y: auto;
  padding: 4px 20px; font-size: 12px; line-height: 1.6;
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
.ln  { color: var(--blue); flex-shrink:0; min-width:100px; max-width:140px; overflow:hidden; }
.lm  { color: var(--text); word-break: break-all; }
.lm.warn-msg { color: var(--yellow); }
.lm.err-msg  { color: var(--red); }
.count-badge {
  font-size: 10px; color: var(--dim);
  padding: 1px 7px; border: 1px solid var(--border);
}

/* ── Modal overlay ── */
.modal {
  position: fixed; inset: 0;
  background: rgba(7,10,14,.92);
  backdrop-filter: blur(4px);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
}
.modal.hidden { display: none; }

.modal-content {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 28px;
  max-height: 90vh;
  overflow-y: auto;
  width: 90%;
  max-width: 600px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
}

.modal-title {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 16px;
  font-weight: bold;
  letter-spacing: 1px;
}

.modal-close {
  background: transparent;
  border: none;
  color: var(--dim);
  font-size: 24px;
  cursor: pointer;
  padding: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: color 0.2s;
}

.modal-close:hover {
  color: var(--text);
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
  background: var(--surface2); border: 1px solid var(--border);
  padding: 24px 28px; width: 420px;
  display: flex; flex-direction: column; gap: 14px;
}
.card-title { font-size: 13px; letter-spacing: 2px; text-transform: uppercase; color: var(--green-hi); }
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
    <div class="hdr-sub">ALPACA CONTROL</div>
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

  <div id="errBanner" style="display:none;color:var(--red);font-size:11px;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title=""></div>

  <div class="hdr-right">
    <button class="btn btn-dim" id="btnHdrTel" onclick="openTelescopeModal()">
      <span class="dot dot-gray" id="telDot" style="vertical-align:middle;margin-right:5px;"></span>Telescope
    </button>
    <button class="btn btn-dim" id="btnHdrCam" onclick="openCameraModal()">
      <span class="dot dot-gray" id="camDot" style="vertical-align:middle;margin-right:5px;"></span>Camera
    </button>
    <button class="btn btn-blue" onclick="showDiscover()">Discover</button>
  </div>
</div>

<!-- Main grid -->
<div class="main" id="mainGrid">

  <!-- Empty state shown when nothing to display -->
  <div class="main-empty" id="mainEmpty" style="grid-column:1/-1;background:var(--surface);">
    <div style="font-size:24px;opacity:0.3">✦</div>
    <div>No active feeds — connect a device to get started</div>
  </div>

  <!-- Pier cam panel -->
  <div class="img-sub hidden" id="pierCamSub">
    <div class="panel-hdr" style="flex-shrink:0">
      <div class="panel-name">
        <span class="dot dot-gray" id="pierCamDot"></span>
        Live View
      </div>
      <div id="pierCamBadge" style="font-size:10px;color:var(--dim)"></div>
    </div>
    <div class="pier-cam-wrap" style="flex:1;min-height:0;">
      <img id="pierCamImg" src="" alt="Pier cam live view" style="max-height:100%;height:100%;object-fit:contain;">
    </div>
    <div class="pier-cam-badge" id="pierCamStatus"></div>
  </div>

  <!-- Last exposure panel -->
  <div class="img-sub hidden" id="lastExpSub">
    <div class="panel-hdr" style="flex-shrink:0">
      <div class="panel-name">Last Exposure</div>
      <div id="imgReadyBadge" style="font-size:10px;color:var(--dim)"></div>
    </div>
    <div class="img-inner" style="flex:1;align-items:stretch;">
      <div class="img-frame" style="max-width:none;flex:1;display:flex;align-items:center;justify-content:center;">
        <img id="lastImg" src="" alt="Last exposure" style="max-width:100%;max-height:100%;object-fit:contain;image-rendering:pixelated;">
      </div>
      <div class="img-meta" id="imgMeta" style="min-width:140px;"></div>
    </div>
  </div>

</div><!-- /main -->

<!-- Telescope Modal -->
<div class="modal hidden" id="telModal" onclick="if(event.target===this)closeTelescopeModal()">
  <div class="modal-content">
    <div class="modal-header">
      <div class="modal-title">
        <span class="dot dot-gray" id="telModalDot"></span>
        🔭 Telescope Control
        <div class="badges" id="telModalBadges" style="margin-left:8px;"></div>
      </div>
      <button class="modal-close" onclick="closeTelescopeModal()">×</button>
    </div>

    <!-- Coordinates -->
    <div style="display: flex; flex-direction: column; gap: 12px; border-bottom: 1px solid var(--border); padding-bottom: 12px;">
      <div style="font-size: 14px; color: var(--dim); letter-spacing: 1px;">CURRENT POSITION</div>
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
        <div>
          <div style="font-size: 11px; color: var(--dim); letter-spacing: 1px; margin-bottom: 4px;">R.A.</div>
          <div class="coord-val" id="telModalRA" style="font-size: 18px;">—</div>
        </div>
        <div>
          <div style="font-size: 11px; color: var(--dim); letter-spacing: 1px; margin-bottom: 4px;">DEC</div>
          <div class="coord-val" id="telModalDec" style="font-size: 18px;">—</div>
        </div>
      </div>
      <div class="coord-raw" id="telModalRaw" style="font-size: 10px;"></div>
    </div>

    <!-- Mount controls -->
    <div class="ctrl-group">
      <div class="panel-label">Mount</div>
      <div class="ctrl-row">
        <button class="btn btn-green" id="btnModalUnpark" onclick="apiUnpark()" disabled>Unpark</button>
        <button class="btn btn-dim"   id="btnModalPark"   onclick="apiPark()"   disabled>Park</button>
      </div>
    </div>

    <!-- Tracking controls -->
    <div class="ctrl-group">
      <div class="panel-label">Tracking</div>
      <div class="ctrl-row">
        <button class="btn btn-green"  id="btnModalTrackOn"  onclick="apiTracking(true)"  disabled>Track ON</button>
        <button class="btn btn-yellow" id="btnModalTrackOff" onclick="apiTracking(false)" disabled>Track OFF</button>
      </div>
    </div>

    <!-- Slew -->
    <div class="ctrl-group">
      <div class="panel-label">Slew Target</div>
      <div class="inp-grid">
        <div class="inp-group">
          <div class="inp-label">R.A. (decimal hours)</div>
          <input class="inp" id="slewRA" type="number" min="0" max="23.9999" step="0.0001" placeholder="0.0000">
        </div>
        <div class="inp-group">
          <div class="inp-label">Dec (decimal degrees)</div>
          <input class="inp" id="slewDec" type="number" min="-90" max="90" step="0.0001" placeholder="0.0000">
        </div>
      </div>
      <button class="btn btn-blue btn-full" id="btnModalSlew" onclick="apiSlew()" disabled>Slew to Target</button>
    </div>

    <!-- Joystick -->
    <div class="ctrl-group">
      <div class="panel-label">Nudge</div>
      <div style="display:flex; align-items:center; gap:16px; flex-wrap:wrap;">
        <div style="display:grid; grid-template-columns:40px 40px 40px; grid-template-rows:40px 40px 40px; gap:4px;">
          <div></div>
          <button class="btn btn-dim joy-btn" id="joyN" onclick="apiNudge('N')" disabled style="padding:0; font-size:16px;" title="North (+Dec)">▲</button>
          <div></div>
          <button class="btn btn-dim joy-btn" id="joyW" onclick="apiNudge('W')" disabled style="padding:0; font-size:16px;" title="West (+RA)">◄</button>
          <div style="display:flex;align-items:center;justify-content:center;color:var(--border);font-size:10px;">✛</div>
          <button class="btn btn-dim joy-btn" id="joyE" onclick="apiNudge('E')" disabled style="padding:0; font-size:16px;" title="East (−RA)">►</button>
          <div></div>
          <button class="btn btn-dim joy-btn" id="joyS" onclick="apiNudge('S')" disabled style="padding:0; font-size:16px;" title="South (−Dec)">▼</button>
          <div></div>
        </div>
        <div style="display:flex; flex-direction:column; gap:8px;">
          <div style="font-size:10px; color:var(--dim); letter-spacing:1px;">STEP SIZE</div>
          <div style="display:flex; flex-direction:column; gap:4px;" id="nudgeStepBtns">
            <button class="btn btn-dim" style="font-size:11px; padding:3px 10px;" onclick="setNudgeStep(10)"   id="nudge10">10″</button>
            <button class="btn btn-dim" style="font-size:11px; padding:3px 10px;" onclick="setNudgeStep(60)"   id="nudge60">1′</button>
            <button class="btn btn-dim" style="font-size:11px; padding:3px 10px;" onclick="setNudgeStep(300)"  id="nudge300">5′</button>
            <button class="btn btn-dim" style="font-size:11px; padding:3px 10px;" onclick="setNudgeStep(900)"  id="nudge900">15′</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Camera Modal -->
<div class="modal hidden" id="camModal" onclick="if(event.target===this)closeCameraModal()">
  <div class="modal-content">
    <div class="modal-header">
      <div class="modal-title">
        <span class="dot dot-gray" id="camModalDot"></span>
        📷 Camera Control
      </div>
      <button class="modal-close" onclick="closeCameraModal()">×</button>
    </div>

    <!-- State display -->
    <div style="display: flex; flex-direction: column; gap: 8px; border-bottom: 1px solid var(--border); padding-bottom: 12px;">
      <div class="cam-state cs-idle" id="camModalState">—</div>
      <div class="cam-sub" id="camModalSub"></div>
      <div id="camModalReady" style="font-size:11px;color:var(--gray)"></div>
    </div>

    <!-- Exposure controls -->
    <div class="ctrl-group">
      <div class="panel-label">Exposure</div>
      <div class="inp-grid">
        <div class="inp-group">
          <div class="inp-label">Duration (seconds)</div>
          <input class="inp" id="expDuration" type="number" min="0.001" step="0.1" value="1.0" placeholder="1.0">
        </div>
        <div class="inp-group">
          <div class="inp-label">Binning</div>
          <input class="inp" id="expBinning" type="number" min="1" max="8" step="1" value="1" placeholder="1">
        </div>
      </div>
      <div class="ctrl-row">
        <button class="btn btn-green" id="btnModalExpose" onclick="apiExpose()" disabled>Expose</button>
        <button class="btn btn-red"   id="btnModalAbortExp" onclick="apiAbortExposure()" disabled>Abort</button>
      </div>
    </div>
  </div>
</div>

<!-- Log footer -->
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
      <button class="btn btn-dim"   onclick="hideDiscover()">Cancel</button>
    </div>
  </div>
</div>

<script>

let _nudgeStep = 60;

// ── Status polling ──────────────────────────────────────────────────────────

async function poll() {
  try {
    const r = await fetch("/api/status");
    render(await r.json());
  } catch {}
}
setInterval(poll, 1000);
poll();
setNudgeStep(60);

function render(s) {
  renderHeader(s);
  renderTelescope(s.telescope || {});
  renderCamera(s.camera || {});
  renderSafety(s.safety || {});
  renderImage(s);
  renderPierCam(s.pier_cam || {});
}

// ── Modal management ────────────────────────────────────────────────────────

function openTelescopeModal() {
  document.getElementById("telModal").classList.remove("hidden");
}

function closeTelescopeModal() {
  document.getElementById("telModal").classList.add("hidden");
}

function openCameraModal() {
  document.getElementById("camModal").classList.remove("hidden");
}

function closeCameraModal() {
  document.getElementById("camModal").classList.add("hidden");
}

// Close modals on escape key
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeTelescopeModal();
    closeCameraModal();
  }
});

// ── Header ──────────────────────────────────────────────────────────────────

function renderHeader(s) {
  const dot   = document.getElementById("connDot");
  const label = document.getElementById("connLabel");

  if (s.server) {
    const srv = document.getElementById("hdrServer");
    srv.classList.remove("hidden");
    document.getElementById("hdrAddr").textContent =
      `${s.server.address}:${s.server.port}`;
  }

  if (s.connected) {
    dot.className    = "dot dot-green";
    label.textContent = "Connected";
  } else {
    dot.className    = "dot dot-gray";
    label.textContent = "Disconnected";
  }

  const errBanner = document.getElementById("errBanner");
  if (s.error) {
    errBanner.style.display = "block";
    errBanner.textContent   = "⚠ " + s.error;
    errBanner.title         = s.error;
  } else {
    errBanner.style.display = "none";
  }
}

// ── Safety ──────────────────────────────────────────────────────────────────

function renderSafety(sf) {
  const pill   = document.getElementById("safetyPill");
  const dot    = document.getElementById("safetyDot");
  const label  = document.getElementById("safetyLabel");
  const reason = document.getElementById("safetyReason");
  const sunEl  = document.getElementById("sunEl");

  if (!sf || sf.safe === undefined) { pill.style.display = "none"; return; }
  pill.style.display = "flex";

  if (sf.safe) {
    dot.className     = "dot dot-green";
    label.textContent = "SAFE";
    label.style.color = "var(--green)";
    reason.textContent = sf.heartbeat_ok ? "" : "hb?";
  } else {
    dot.className     = "dot dot-red pulse";
    label.textContent = "UNSAFE";
    label.style.color = "var(--red)";
    reason.textContent = sf.reason ? `· ${sf.reason}` : "";
  }

  if (sf.sun_elevation != null) {
    const el  = sf.sun_elevation.toFixed(1);
    const thr = sf.dawn_threshold != null ? sf.dawn_threshold.toFixed(0) : "-18";
    sunEl.style.color = sf.sun_elevation > sf.dawn_threshold ? "var(--yellow)" : "var(--dim)";
    sunEl.textContent = `☀ ${el >= 0 ? "+" : ""}${el}°`;
    sunEl.title       = `Sun elevation (dawn at ${thr}°)`;
  } else {
    sunEl.textContent = "";
  }
}

// ── Telescope ───────────────────────────────────────────────────────────────

function renderTelescope(t) {
  const dotCls = t.connected ? "dot dot-green" : "dot dot-gray";
  document.getElementById("telDot").className      = dotCls;
  document.getElementById("telModalDot").className = dotCls;

  // Update header button style to show connection state
  const hdrBtn = document.getElementById("btnHdrTel");
  if (t.connected)       hdrBtn.className = "btn btn-green";
  else if (t.enabled)    hdrBtn.className = "btn btn-red";
  else                   hdrBtn.className = "btn btn-dim";

  const raEl  = document.getElementById("telModalRA");
  const decEl = document.getElementById("telModalDec");
  const rawEl = document.getElementById("telModalRaw");

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

  // Badges in modal header
  const badges = document.getElementById("telModalBadges");
  if (badges) {
    badges.innerHTML = "";
    if (t.connected) {
      if (t.busy)     badges.innerHTML += `<span class="badge badge-warn pulse">Busy</span>`;
      if (t.slewing)  badges.innerHTML += `<span class="badge badge-warn pulse">Slewing</span>`;
      if (t.tracking) badges.innerHTML += `<span class="badge badge-on">Tracking</span>`;
      if (t.parked)   badges.innerHTML += `<span class="badge badge-warn">Parked</span>`;
      if (!t.busy && !t.slewing && !t.tracking && !t.parked)
                      badges.innerHTML += `<span class="badge">Idle</span>`;
    } else if (t.enabled) {
      badges.innerHTML += `<span class="badge badge-err">Disconnected</span>`;
    }
  }

  const blocked = !t.connected || t.busy || t.slewing;
  document.getElementById("btnModalUnpark").disabled   = blocked;
  document.getElementById("btnModalPark").disabled     = blocked;
  document.getElementById("btnModalTrackOn").disabled  = blocked;
  document.getElementById("btnModalTrackOff").disabled = blocked;
  document.getElementById("btnModalSlew").disabled     = blocked;
  ["joyN","joyS","joyE","joyW"].forEach(id => {
    document.getElementById(id).disabled = blocked;
  });
}

// ── Camera ───────────────────────────────────────────────────────────────────

const CAM_CLASSES = ["cs-idle","cs-wait","cs-expose","cs-read","cs-dl","cs-error"];

function renderCamera(c) {
  const dotCls = c.connected ? "dot dot-green" : "dot dot-gray";
  document.getElementById("camDot").className      = dotCls;
  document.getElementById("camModalDot").className = dotCls;

  // Update header button style
  const hdrBtn = document.getElementById("btnHdrCam");
  if (c.connected && c.exposing) hdrBtn.className = "btn btn-yellow";
  else if (c.connected)          hdrBtn.className = "btn btn-green";
  else if (c.enabled)            hdrBtn.className = "btn btn-red";
  else                           hdrBtn.className = "btn btn-dim";

  const stEl  = document.getElementById("camModalState");
  const subEl = document.getElementById("camModalSub");
  const rdEl  = document.getElementById("camModalReady");

  if (c.connected) {
    stEl.textContent = (c.exposing ? (c.state_name || "Exposing") : (c.state_name || "—")).toUpperCase();
    stEl.className   = "cam-state " + (CAM_CLASSES[c.state] || "cs-idle");
    if (c.state === 2 || c.exposing) stEl.classList.add("pulse");
    subEl.textContent = c.exposing ? `ALPACA state ${c.state} · exposure in progress` : `ALPACA state ${c.state}`;
    rdEl.textContent  = c.image_ready ? "✓ IMAGE READY" : "";
    rdEl.style.color  = c.image_ready ? "var(--green)" : "var(--gray)";
  } else {
    stEl.textContent = "—"; stEl.className = "cam-state cs-idle";
    subEl.textContent = c.enabled ? "Disconnected" : "Not enabled";
    rdEl.textContent  = "";
  }

  // Also reflect image-ready in main area badge
  const imgReadyBadge = document.getElementById("imgReadyBadge");
  if (imgReadyBadge) {
    imgReadyBadge.textContent = c.image_ready ? "✓ IMAGE READY" : "";
    imgReadyBadge.style.color = c.image_ready ? "var(--green)" : "var(--dim)";
  }

  document.getElementById("btnModalExpose").disabled   = !c.connected || c.exposing;
  document.getElementById("btnModalAbortExp").disabled = !c.connected || !c.exposing;
}

// ── Main area visibility ──────────────────────────────────────────────────────

function updateImgRow() {
  const pier  = document.getElementById("pierCamSub");
  const last  = document.getElementById("lastExpSub");
  const empty = document.getElementById("mainEmpty");
  const hasContent = !pier.classList.contains("hidden") || !last.classList.contains("hidden");
  empty.style.display = hasContent ? "none" : "flex";
}

// ── Image ────────────────────────────────────────────────────────────────────

let _lastImageId = -1;

async function renderImage(s) {
  if (!s.image_captured) return;
  if (s.image_id === _lastImageId) return;
  _lastImageId = s.image_id;

  const sub  = document.getElementById("lastExpSub");
  const img  = document.getElementById("lastImg");
  const meta = document.getElementById("imgMeta");

  sub.classList.remove("hidden");
  updateImgRow();
  meta.innerHTML = "Downloading…";

  try {
    const r    = await fetch("/api/image");
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
    _lastImageId = -1;
  }
}

// ── Pier cam ──────────────────────────────────────────────────────────────────

let _pierCamConnected = false;

function renderPierCam(pc) {
  if (!pc || !pc.enabled) return;

  const sub    = document.getElementById("pierCamSub");
  const dot    = document.getElementById("pierCamDot");
  const badge  = document.getElementById("pierCamBadge");
  const status = document.getElementById("pierCamStatus");
  const img    = document.getElementById("pierCamImg");

  sub.classList.remove("hidden");
  updateImgRow();

  if (pc.error) {
    dot.className     = "dot dot-red";
    badge.textContent = "Error";
    badge.style.color = "var(--red)";
    status.textContent = pc.error;
    status.style.color = "var(--red)";
  } else if (pc.streaming) {
    dot.className     = "dot dot-green";
    badge.textContent = "Live";
    badge.style.color = "var(--green)";
    status.textContent = "";
    if (!_pierCamConnected) {
      _pierCamConnected = true;
      img.src = "/api/pier-cam/stream";
      img.onerror = () => _pierCamRetry();
    }
  } else {
    dot.className     = "dot dot-yellow pulse";
    badge.textContent = "Initializing";
    badge.style.color = "var(--dim)";
    status.textContent = "";
  }
}

function _pierCamRetry() {
  _pierCamConnected = false;
  const status = document.getElementById("pierCamStatus");
  status.textContent = "Reconnecting…";
  status.style.color = "var(--dim)";
  setTimeout(() => {
    const img = document.getElementById("pierCamImg");
    img.src = "/api/pier-cam/stream?" + Date.now();
    img.onerror = () => _pierCamRetry();
    _pierCamConnected = true;
  }, 2000);
}

// ── Coordinate formatters ────────────────────────────────────────────────────

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
  const abs  = Math.abs(d);
  const deg  = Math.floor(abs);
  const mn   = Math.floor((abs - deg) * 60);
  const sec  = ((abs - deg) * 3600 - mn * 60).toFixed(1);
  return `${sign}${pad(deg)}° ${pad(mn)}' ${String(sec).padStart(4,"0")}"`;
}

function pad(n) { return String(n).padStart(2, "0"); }

// ── Telescope actions ────────────────────────────────────────────────────────

async function apiUnpark() {
  try { await fetch("/api/telescope/unpark", { method: "POST" }); }
  catch (e) { alert("Unpark failed: " + e.message); }
}

async function apiPark() {
  try { await fetch("/api/telescope/park", { method: "POST" }); }
  catch (e) { alert("Park failed: " + e.message); }
}

async function apiTracking(enabled) {
  try {
    await fetch("/api/telescope/tracking", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  } catch (e) { alert("Set tracking failed: " + e.message); }
}

let _nudgeStep = 60;

function setNudgeStep(arcsec) {
  _nudgeStep = arcsec;
  ["10","60","300","900"].forEach(v => {
    const el = document.getElementById("nudge" + v);
    if (el) el.style.borderColor = (parseInt(v) === arcsec) ? "var(--blue)" : "";
    if (el) el.style.color       = (parseInt(v) === arcsec) ? "var(--blue)" : "";
  });
}

async function apiNudge(direction) {
  try {
    const r = await fetch("/api/telescope/nudge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction, step: _nudgeStep }),
    });
    const d = await r.json();
    if (!d.ok) alert(d.error || "Nudge failed");
  } catch (e) { alert("Nudge failed: " + e.message); }
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
  } catch (e) { alert("Slew failed: " + e.message); }
  btn.textContent = "Slew to Target";
  // disabled state re-evaluated on next poll
}

// ── Camera actions ───────────────────────────────────────────────────────────

async function apiExpose() {
  const duration = parseFloat(document.getElementById("expDuration").value);
  const binning  = parseInt(document.getElementById("expBinning").value);
  if (isNaN(duration) || duration <= 0) { alert("Enter a valid exposure duration > 0 s."); return; }
  if (isNaN(binning) || binning < 1)    { alert("Binning must be >= 1."); return; }
  try {
    const r = await fetch("/api/camera/expose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ duration, binning }),
    });
    const d = await r.json();
    if (!d.ok) alert(d.error || "Expose failed");
  } catch (e) { alert("Expose failed: " + e.message); }
}

async function apiAbortExposure() {
  try { await fetch("/api/camera/abort", { method: "POST" }); }
  catch (e) { alert("Abort failed: " + e.message); }
}

// ── Log stream (SSE) ─────────────────────────────────────────────────────────

let logCount   = 0;
let autoScroll = true;
const logBody  = document.getElementById("logBody");

logBody.addEventListener("scroll", () => {
  autoScroll = logBody.scrollTop + logBody.clientHeight >= logBody.scrollHeight - 24;
});

function appendLog(entry) {
  logCount++;
  document.getElementById("logCount").textContent = logCount + " lines";

  const raw   = entry.msg || "";
  const match = raw.match(/^\S+\s+\[\w+\]\s+([^:]+):\s(.*)/s);
  const name  = match ? match[1] : (entry.name || "");
  const msg   = match ? match[2] : raw;

  const line = document.createElement("div");
  line.className = "ll";

  const t  = document.createElement("span"); t.className = "lt"; t.textContent = entry.time || "";
  const lv = document.createElement("span"); lv.className = "llv " + entry.level;
  lv.textContent = "[" + (entry.level || "").substring(0, 4) + "]";
  const nm = document.createElement("span"); nm.className = "ln"; nm.textContent = name;
  const ms = document.createElement("span"); ms.className = "lm";
  if (entry.level === "WARNING") ms.classList.add("warn-msg");
  if (entry.level === "ERROR")   ms.classList.add("err-msg");
  ms.textContent = msg;

  line.appendChild(t); line.appendChild(lv); line.appendChild(nm); line.appendChild(ms);
  logBody.appendChild(line);
  if (autoScroll) logBody.scrollTop = logBody.scrollHeight;
}

function clearLog() {
  logBody.innerHTML = ""; logCount = 0;
  document.getElementById("logCount").textContent = "0 lines";
}

const es = new EventSource("/api/logs");
es.onmessage = e => { try { appendLog(JSON.parse(e.data)); } catch {} };

// ── Discovery overlay ────────────────────────────────────────────────────────

function showDiscover()  { document.getElementById("overlay").classList.remove("hidden"); }
function hideDiscover()  { document.getElementById("overlay").classList.add("hidden"); }

async function doScan() {
  const btn = document.getElementById("scanBtn");
  btn.textContent = "Scanning…"; btn.disabled = true;
  document.getElementById("srvList").innerHTML = "";
  try {
    const r    = await fetch("/api/discover", { method: "POST" });
    const data = await r.json();
    const list = document.getElementById("srvList");
    if (data.servers?.length) {
      data.servers.forEach(srv => {
        const item = document.createElement("div");
        item.className   = "srv-item";
        item.textContent = `${srv.address}:${srv.port}`;
        item.onclick     = () => connectTo(srv.address, srv.port);
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
  } catch (e) {
    alert("Connection failed: " + e.message);
  }
}
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

def launch(port: int = 5173) -> None:
    global _safety_mgr, _image_watcher

    import urllib.request
    import webbrowser

    cfg = _load_config()
    log_cfg = cfg.get("logging", {})
    logging.basicConfig(
        level=log_cfg.get("level", "INFO"),
        format=log_cfg.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    )

    _safety_mgr = SafetyManager(config=cfg, on_unsafe=_on_safety_unsafe)
    _safety_mgr.start()

    iw_cfg = cfg.get("image_watcher", {})
    if iw_cfg.get("enabled", False):
        watch_path     = iw_cfg.get("watch_path", "/mnt/seestar")
        debounce_delay = float(iw_cfg.get("debounce_delay", 2.0))
        _image_watcher = ImageWatcher(watch_path, _on_new_fits, debounce_delay)
        _image_watcher.start()
        with _state_lock:
            _state["image_watcher"]["enabled"]    = True
            _state["image_watcher"]["watch_path"] = watch_path

    phot_cfg = cfg.get("photometry", {})
    if phot_cfg.get("enabled", False):
        with _state_lock:
            _state["photometry"]["enabled"] = True
        logger.info("Photometry pipeline enabled (node_id=%s)", phot_cfg.get("node_id", "?"))

    pc_cfg = cfg.get("pier_cam", {})
    if pc_cfg.get("enabled", False):
        _pier_cam_stop.clear()
        threading.Thread(target=_pier_cam_loop, daemon=True, name="pier-cam").start()
        with _state_lock:
            _state["pier_cam"]["enabled"] = True

    flask_thread = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0", port=port, debug=False,
            threaded=True, use_reloader=False,
        ),
        daemon=True,
        name="flask",
    )
    flask_thread.start()

    url = f"http://localhost:{port}"
    for _ in range(20):
        try:
            urllib.request.urlopen(url, timeout=0.5)
            break
        except Exception:
            time.sleep(0.25)

    print(f"\n  NODE v1  →  {url}\n", file=sys.__stdout__)
    webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down.", file=sys.__stdout__)
    finally:
        _pier_cam_stop.set()
        if _safety_mgr is not None:
            _safety_mgr.stop()
        if _image_watcher is not None:
            _image_watcher.stop()


if __name__ == "__main__":
    launch()
