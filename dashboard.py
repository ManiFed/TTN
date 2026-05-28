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
from flask import Flask, Response, jsonify, render_template_string, request, send_from_directory, stream_with_context

from alpaca.discovery import discover_servers
from alpaca.safety_manager import SafetyManager
from alpaca.telescope import Telescope
from alpaca.camera import Camera
from image_watcher import ImageWatcher
from photometry import run_pipeline as _run_photometry
from aavso_submission import submit as _aavso_submit
from fits_export import export_enhanced_fits as _export_fits


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
        "enabled":     False,
        "last_result": None,   # most recent measurement dict
        "last_export": None,   # path to most recent exported FITS file
        "running":     False,
    },
    "aavso": {
        "last_submission": None,   # most recent submit() result dict
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
            export_cfg = cfg.get("photometry", {}).get("fits_export", {})
            if export_cfg.get("enabled", True):
                export_path = _export_fits(fits_path, result, cfg)
                with _state_lock:
                    _state["photometry"]["last_export"] = export_path
            if cfg.get("aavso", {}).get("observer_code", "").strip():
                sub = _aavso_submit(result, cfg)
                with _state_lock:
                    _state["aavso"]["last_submission"] = sub
                logger.info(
                    "AAVSO submission: status=%s accepted=%d rejected=%d — %s",
                    sub["status"], sub["accepted"], sub["rejected"], sub["message"],
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


# ── Horizon scan state ────────────────────────────────────────────────────────

_scan_lock  = threading.Lock()
_scan_state: dict = {
    "running":    False,
    "cancelled":  False,
    "directions": [],   # list of 12 dicts: {az, status, horizon_alt, steps}
    "result":     None, # [[alt, az], …] on completion
    "error":      None,
}


def _count_stars_in_array(image_array) -> int:
    """Return a source count from a raw image array (nested list or ndarray)."""
    try:
        import numpy as np
        from photutils.detection import DAOStarFinder
        from astropy.stats import sigma_clipped_stats

        data = np.array(image_array, dtype=np.float64)
        if data.ndim == 3:          # colour / 3-axis ALPACA response → take first plane
            data = data[0]
        _, median, std = sigma_clipped_stats(data, sigma=3.0)
        if std <= 0:
            return 0
        finder = DAOStarFinder(fwhm=4.0, threshold=5.0 * std, exclude_border=True)
        sources = finder(data - median)
        return len(sources) if sources is not None else 0
    except Exception as exc:
        logger.debug("_count_stars_in_array error: %s", exc)
        # Crude fallback: count pixels > 8σ above mean
        try:
            import numpy as np
            data = np.array(image_array, dtype=np.float64)
            if data.ndim == 3:
                data = data[0]
            m, s = float(data.mean()), float(data.std())
            return int((data > m + 8 * s).sum()) if s > 0 else 0
        except Exception:
            return 0


def _wait_slew_complete(timeout: float = 120.0) -> None:
    """Block until the telescope stops slewing (or times out)."""
    # Brief window for the async command to be accepted and slewing to begin
    start = time.monotonic()
    while time.monotonic() - start < 6:
        try:
            if _tel and _tel.is_slewing():
                break
        except Exception:
            pass
        time.sleep(0.25)
    # Now wait for it to finish
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _tel and not _tel.is_slewing():
                return
        except Exception:
            pass
        time.sleep(0.5)


def _scan_slew_to(alt: float, az: float) -> None:
    """
    Slew to an Alt/Az position for the horizon scan.
    Tries native Alt/Az first; falls back to RA/Dec conversion via astropy.
    Blocks until the slew completes.
    """
    try:
        _tel.begin_slew_altaz(alt, az)
        _wait_slew_complete()
        return
    except Exception as exc:
        logger.debug("Alt/Az slew failed (%s) — trying RA/Dec fallback", exc)

    # RA/Dec fallback
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    import astropy.units as u
    cfg = _load_config()
    obs = cfg.get("safety", {}).get("observer", {})
    lat = float(obs.get("latitude", 0.0))
    lon = float(obs.get("longitude", 0.0))
    loc   = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)
    frame = AltAz(obstime=Time.now(), location=loc)
    coord = SkyCoord(alt=alt * u.deg, az=az * u.deg, frame=frame)
    eq    = coord.icrs
    _tel.slew_to_coordinates(float(eq.ra.deg) / 15.0, float(eq.dec.deg))


def _run_horizon_scan(
    floor_alt: float,
    start_alt: float,
    step_deg:  float,
    exposure_s: float,
    star_threshold: int,
    settle_s: float,
) -> None:
    """Background thread: slew to 12 azimuths × N altitudes, count stars, build mask."""
    import numpy as np

    N       = 12
    AZ_STEP = 30

    directions = [
        {"az": i * AZ_STEP, "status": "pending", "horizon_alt": None, "steps": []}
        for i in range(N)
    ]
    with _scan_lock:
        _scan_state.update({
            "running":    True,
            "cancelled":  False,
            "directions": directions,
            "result":     None,
            "error":      None,
        })

    result_alts: list[float] = []

    try:
        for i in range(N):
            az = i * AZ_STEP

            with _scan_lock:
                if _scan_state["cancelled"]:
                    _scan_state["running"] = False
                    return
                _scan_state["directions"][i]["status"] = "scanning"

            altitudes = list(np.arange(start_alt, floor_alt - 1e-6, -step_deg))
            if not altitudes or altitudes[-1] > floor_alt + 1e-6:
                altitudes.append(floor_alt)

            last_clear_alt: Optional[float] = None

            for alt in altitudes:
                with _scan_lock:
                    if _scan_state["cancelled"]:
                        _scan_state["running"] = False
                        return

                # ── slew ──────────────────────────────────────────────────────
                try:
                    _scan_slew_to(alt, az)
                except Exception as exc:
                    logger.error("Scan: slew to Alt=%.1f Az=%.1f failed: %s", alt, az, exc)
                    with _scan_lock:
                        _scan_state["directions"][i]["steps"].append(
                            {"alt": alt, "stars": None, "error": str(exc)}
                        )
                    continue

                time.sleep(settle_s)   # vibration settle

                # ── expose + count ────────────────────────────────────────────
                stars: Optional[int] = None
                try:
                    _cam.expose(exposure_s, readout_timeout=60.0)
                    img = _cam.image_array()
                    stars = _count_stars_in_array(img)
                    logger.info("Scan: Alt=%.1f Az=%.1f → %d stars", alt, az, stars)
                except Exception as exc:
                    logger.error("Scan: exposure at Alt=%.1f Az=%.1f failed: %s", alt, az, exc)

                with _scan_lock:
                    _scan_state["directions"][i]["steps"].append(
                        {"alt": alt, "stars": stars}
                    )

                if stars is not None and stars >= star_threshold:
                    last_clear_alt = alt
                elif last_clear_alt is not None:
                    # Transition found: had sky, now blocked → stop descending
                    break

            # ── derive horizon altitude for this azimuth ──────────────────────
            if last_clear_alt is None:
                # Never saw sky from start_alt down — fully blocked
                horizon_alt = round(start_alt, 1)
            elif last_clear_alt <= floor_alt + 1e-6:
                # Still clear at the hardware floor — horizon is below it
                horizon_alt = 0.0
            else:
                horizon_alt = round(last_clear_alt, 1)

            result_alts.append(horizon_alt)
            with _scan_lock:
                _scan_state["directions"][i]["status"]      = "done"
                _scan_state["directions"][i]["horizon_alt"] = horizon_alt

        result = [[result_alts[i], i * AZ_STEP] for i in range(N)]
        with _scan_lock:
            _scan_state["result"]  = result
            _scan_state["running"] = False
        logger.info("Horizon scan complete: %s", result)

    except Exception as exc:
        logger.error("Horizon scan crashed: %s", exc)
        with _scan_lock:
            _scan_state["error"]   = str(exc)
            _scan_state["running"] = False


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


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    global _tel, _cam
    _poller_stop.set()
    with _state_lock:
        _state["connected"] = False
        _state["telescope"]["connected"] = False
        _state["camera"]["connected"] = False
        _state["server"] = None
    try:
        if _tel is not None:
            _tel.disconnect()
    except Exception:
        pass
    try:
        if _cam is not None:
            _cam.disconnect()
    except Exception:
        pass
    _tel = None
    _cam = None
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
    mode = data.get("mode", "eq")

    if mode == "altaz":
        try:
            alt = float(data["alt"])
            az  = float(data["az"])
        except (KeyError, ValueError):
            return jsonify({"error": "Invalid alt/az"}), 400
        if not (0.0 <= alt <= 90.0):
            return jsonify({"error": "Altitude must be in range [0, 90]"}), 400
        if not (0.0 <= az < 360.0):
            return jsonify({"error": "Azimuth must be in range [0, 360)"}), 400

        # Horizon-mask check
        if _safety_mgr is not None and not _safety_mgr.is_pointing_safe(alt, az):
            min_alt = _safety_mgr.min_safe_altitude(az)
            msg = (
                f"Slew rejected by horizon mask: "
                f"Alt {alt:.1f}° is below the {min_alt:.1f}° limit at Az {az:.1f}°"
            )
            logger.warning(msg)
            return jsonify({"error": msg, "horizon_blocked": True,
                            "min_safe_alt": round(min_alt, 1)}), 403

        try:
            _tel.begin_slew_altaz(alt, az)
        except Exception as exc:
            logger.warning("Alt-Az slew not supported by driver (%s) — converting to RA/Dec", exc)
            cfg = _load_config()
            obs = cfg.get("safety", {}).get("observer", {})
            lat = float(obs.get("latitude", 0.0))
            lon = float(obs.get("longitude", 0.0))
            try:
                from astropy.coordinates import AltAz, EarthLocation, SkyCoord
                from astropy.time import Time
                import astropy.units as u
                location = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)
                t = Time.now()
                altaz_frame = AltAz(obstime=t, location=location)
                coord = SkyCoord(alt=alt * u.deg, az=az * u.deg, frame=altaz_frame)
                eq = coord.icrs
                ra_h = float(eq.ra.deg) / 15.0
                dec_d = float(eq.dec.deg)
                _tel.begin_slew(ra_h, dec_d)
                logger.info("Alt-Az fallback slew: RA=%.4f h  Dec=%.4f °", ra_h, dec_d)
            except Exception as exc2:
                logger.error("Alt-Az fallback slew failed: %s", exc2)
                return jsonify({"error": str(exc2)}), 500
    else:
        try:
            ra  = float(data["ra"])
            dec = float(data["dec"])
        except (KeyError, ValueError):
            return jsonify({"error": "Invalid ra/dec"}), 400
        if not (0.0 <= ra < 24.0):
            return jsonify({"error": "RA must be in range [0, 24)"}), 400
        if not (-90.0 <= dec <= 90.0):
            return jsonify({"error": "Dec must be in range [-90, 90]"}), 400

        # Horizon-mask check — convert RA/Dec → Alt/Az for this site & time
        if _safety_mgr is not None and _safety_mgr._horizon_mask:
            cfg_now = _load_config()
            obs_now = cfg_now.get("safety", {}).get("observer", {})
            lat_now = float(obs_now.get("latitude", 0.0))
            lon_now = float(obs_now.get("longitude", 0.0))
            if lat_now != 0.0 or lon_now != 0.0:
                try:
                    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
                    from astropy.time import Time
                    import astropy.units as u
                    loc = EarthLocation(lat=lat_now * u.deg, lon=lon_now * u.deg)
                    frame = AltAz(obstime=Time.now(), location=loc)
                    coord = SkyCoord(ra=ra * 15.0 * u.deg, dec=dec * u.deg).transform_to(frame)
                    t_alt = float(coord.alt.deg)
                    t_az  = float(coord.az.deg)
                    if not _safety_mgr.is_pointing_safe(t_alt, t_az):
                        min_alt = _safety_mgr.min_safe_altitude(t_az)
                        msg = (
                            f"Slew rejected by horizon mask: "
                            f"Alt {t_alt:.1f}° is below the {min_alt:.1f}° limit at Az {t_az:.1f}°"
                        )
                        logger.warning(msg)
                        return jsonify({"error": msg, "horizon_blocked": True,
                                        "min_safe_alt": round(min_alt, 1)}), 403
                except Exception as exc_mask:
                    logger.debug("Horizon-mask RA/Dec check skipped: %s", exc_mask)

        try:
            _tel.begin_slew(ra, dec)
        except Exception as exc:
            logger.error("Slew failed: %s", exc)
            return jsonify({"error": str(exc)}), 500

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

    try:
        _tel.begin_slew(new_ra, new_dec)
    except Exception as exc:
        logger.error("Nudge slew failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    logger.info("Nudge %s %.0f\" → RA=%.4f h  Dec=%.4f °", direction, step_arcsec, new_ra, new_dec)
    return jsonify({"ok": True})


@app.route("/api/telescope/moveaxis", methods=["POST"])
def api_move_axis():
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400
    data = request.get_json(force=True) or {}
    try:
        ra_rate  = float(data.get("ra_rate",  0))
        dec_rate = float(data.get("dec_rate", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400
    try:
        _tel.move_axis(0, ra_rate)
        _tel.move_axis(1, dec_rate)
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("MoveAxis failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


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
            "last_export": _state["photometry"]["last_export"],
        }
    return jsonify(snap)


@app.route("/api/fits/list")
def api_fits_list():
    cfg        = _load_config()
    export_dir = cfg.get("photometry", {}).get("fits_export", {}).get("export_dir", "fits_export")
    files = []
    if os.path.isdir(export_dir):
        for date_dir in sorted(os.scandir(export_dir), key=lambda e: e.name, reverse=True):
            if not date_dir.is_dir():
                continue
            for entry in sorted(os.scandir(date_dir.path), key=lambda e: e.name, reverse=True):
                if not entry.name.lower().endswith((".fits", ".fit")):
                    continue
                obj = date_obs = ""
                try:
                    from astropy.io import fits as _fits
                    with _fits.open(entry.path, memmap=False, ignore_missing_simple=True) as hdul:
                        obj      = str(hdul[0].header.get("OBJECT", ""))
                        date_obs = str(hdul[0].header.get("DATE-OBS", ""))
                except Exception:
                    pass
                files.append({
                    "filename": entry.name,
                    "date":     date_dir.name,
                    "size_kb":  round(entry.stat().st_size / 1024, 1),
                    "object":   obj,
                    "date_obs": date_obs,
                    "path":     os.path.relpath(entry.path),
                })
    return jsonify({"files": files})


@app.route("/api/fits/download/<path:filename>")
def api_fits_download(filename: str):
    cfg        = _load_config()
    export_dir = cfg.get("photometry", {}).get("fits_export", {}).get("export_dir", "fits_export")
    export_abs = os.path.realpath(export_dir)
    target_abs = os.path.realpath(os.path.join(export_abs, filename))
    if not target_abs.startswith(export_abs + os.sep):
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isfile(target_abs):
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(
        os.path.dirname(target_abs),
        os.path.basename(target_abs),
        as_attachment=True,
        mimetype="application/fits",
    )


@app.route("/api/aavso")
def api_aavso():
    with _state_lock:
        snap = dict(_state["aavso"])
    return jsonify(snap)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    try:
        with open("config.yaml") as fh:
            return fh.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except FileNotFoundError:
        return "", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/config", methods=["POST"])
def api_config_post():
    raw = request.get_data(as_text=True)
    try:
        yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        with open("config.yaml", "w") as fh:
            fh.write(raw)
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    logger.info("config.yaml updated via dashboard")
    return jsonify({"ok": True})


@app.route("/api/config/parsed", methods=["GET"])
def api_config_parsed_get():
    return jsonify(_load_config())


@app.route("/api/config/parsed", methods=["POST"])
def api_config_parsed_post():
    data = request.get_json(force=True)
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400
    try:
        raw = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        with open("config.yaml", "w") as fh:
            fh.write(raw)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    logger.info("config.yaml updated via dashboard (form)")
    return jsonify({"ok": True})


@app.route("/api/safety/horizon-mask", methods=["GET"])
def api_horizon_mask_get():
    cfg  = _load_config()
    mask = cfg.get("safety", {}).get("horizon_mask", [])
    return jsonify({"polygon": mask or []})


@app.route("/api/safety/horizon-mask", methods=["POST"])
def api_horizon_mask_post():
    data = request.get_json(force=True) or {}
    polygon = data.get("polygon", [])
    for pt in polygon:
        if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
            return jsonify({"error": "Each point must be [alt, az]"}), 400
        alt, az = float(pt[0]), float(pt[1])
        if not (0.0 <= alt <= 90.0):
            return jsonify({"error": f"Altitude must be 0-90: {alt}"}), 400
        if not (0.0 <= az < 360.0):
            return jsonify({"error": f"Azimuth must be 0-360: {az}"}), 400
    cfg = _load_config()
    if "safety" not in cfg or cfg["safety"] is None:
        cfg["safety"] = {}
    if polygon:
        cfg["safety"]["horizon_mask"] = [[float(p[0]), float(p[1])] for p in polygon]
    else:
        cfg["safety"].pop("horizon_mask", None)
    try:
        with open("config.yaml", "w") as fh:
            yaml.dump(cfg, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    logger.info("Horizon mask updated in config.yaml: %d vertices", len(polygon))
    return jsonify({"ok": True})


@app.route("/api/safety/horizon-scan", methods=["POST"])
def api_horizon_scan_start():
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400
    if _cam is None:
        return jsonify({"error": "Camera not connected"}), 400
    with _scan_lock:
        if _scan_state["running"]:
            return jsonify({"error": "Scan already running"}), 409

    data        = request.get_json(force=True) or {}
    floor_alt   = max(5.0,  min(45.0, float(data.get("floor_alt",   25.0))))
    start_alt   = max(30.0, min(85.0, float(data.get("start_alt",   60.0))))
    step_deg    = max(2.0,  min(15.0, float(data.get("step",         5.0))))
    exposure_s  = max(1.0,  min(60.0, float(data.get("exposure",     5.0))))
    star_thresh = max(1,    min(100,  int(  data.get("star_threshold", 5))))
    settle_s    = max(0.0,  min(15.0, float(data.get("settle",        2.0))))

    if start_alt <= floor_alt:
        return jsonify({"error": "start_alt must be greater than floor_alt"}), 400

    t = threading.Thread(
        target=_run_horizon_scan,
        args=(floor_alt, start_alt, step_deg, exposure_s, star_thresh, settle_s),
        daemon=True,
        name="horizon-scan",
    )
    t.start()
    logger.info(
        "Horizon scan started (floor=%.1f start=%.1f step=%.1f exp=%.1fs thresh=%d settle=%.1fs)",
        floor_alt, start_alt, step_deg, exposure_s, star_thresh, settle_s,
    )
    return jsonify({"ok": True})


@app.route("/api/safety/horizon-scan", methods=["DELETE"])
def api_horizon_scan_cancel():
    with _scan_lock:
        if not _scan_state["running"]:
            return jsonify({"ok": True, "message": "No scan running"})
        _scan_state["cancelled"] = True
    logger.info("Horizon scan cancellation requested")
    return jsonify({"ok": True})


@app.route("/api/safety/horizon-scan/status", methods=["GET"])
def api_horizon_scan_status():
    with _scan_lock:
        return jsonify(dict(_scan_state))


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

.conn-pill-wrap {
  position: relative;
}
.conn-pill {
  display: flex; align-items: center; gap: 5px;
  font-size: 11px; letter-spacing: 1px;
}
.conn-pill.clickable {
  cursor: pointer;
  padding: 3px 8px;
  border: 1px solid var(--green);
  border-radius: 2px;
  color: var(--green);
  user-select: none;
}
.conn-pill.clickable:hover {
  background: rgba(0,255,128,0.08);
}
.conn-dropdown {
  display: none;
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  background: var(--panel);
  border: 1px solid var(--border);
  padding: 6px;
  z-index: 200;
  min-width: 120px;
}
.conn-dropdown.open {
  display: block;
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
  overflow-x: visible;
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
  background: rgba(0,0,0,0.72);
  backdrop-filter: blur(4px) brightness(0.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 50;
}
.overlay.hidden { display: none; }
.card {
  background: var(--surface2); border: 1px solid var(--border);
  padding: 24px 28px; width: 420px;
  display: flex; flex-direction: column; gap: 14px;
  max-height: 90vh; overflow: hidden;
}
.card-title { font-size: 13px; letter-spacing: 2px; text-transform: uppercase; color: var(--green-hi); }
.inp-row { display: flex; gap: 8px; }
.srv-list { display: flex; flex-direction: column; gap: 5px; max-height: 320px; overflow-y: auto; }
.srv-item {
  padding: 7px 12px; border: 1px solid var(--border);
  cursor: pointer; color: var(--blue); transition: border-color .12s, background .12s;
}
.srv-item:hover { border-color: var(--blue); background: rgba(88,166,255,.06); }
.sep { border-top: 1px solid var(--border); }

/* ── Config editor ── */
.cfg-modal .modal-content {
  max-width: 780px;
  width: 95%;
  max-height: 90vh;
  gap: 10px;
}
.cfg-textarea {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.55;
  padding: 10px 14px;
  resize: none;
  width: 100%;
  flex: 1;
  min-height: 0;
  tab-size: 2;
  outline: none;
  transition: border-color 0.15s;
}
.cfg-textarea:focus { border-color: var(--blue); }
.cfg-error {
  color: var(--red);
  font-size: 11px;
  min-height: 14px;
  white-space: pre-wrap;
  word-break: break-all;
}
.cfg-tab {
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--dim);
  cursor: pointer;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 1px;
  padding: 7px 12px;
  text-transform: uppercase;
  transition: color 0.12s, border-color 0.12s;
  white-space: nowrap;
}
.cfg-tab:hover { color: var(--text); }
.cfg-tab.active { color: var(--green-hi); border-bottom-color: var(--green); }
.cfg-panel {
  padding: 14px 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.cfg-section-hdr {
  font-size: 10px;
  letter-spacing: 2px;
  color: var(--dim);
  text-transform: uppercase;
  border-bottom: 1px solid var(--border);
  padding-bottom: 5px;
  margin-top: 6px;
}
.cfg-panel > .cfg-section-hdr:first-child { margin-top: 0; }
.cfg-field-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.cfg-device-row {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  padding: 6px 0;
  border-bottom: 1px solid var(--border);
}
.cfg-device-row:last-of-type { border-bottom: none; }
.cfg-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}
.cfg-toggle input[type="checkbox"] {
  width: 15px;
  height: 15px;
  accent-color: var(--green);
  cursor: pointer;
  flex-shrink: 0;
}
.cfg-toggle span { font-size: 12px; color: var(--text); }
.cfg-toggle-lg { margin-bottom: 2px; }
.cfg-toggle-lg span { font-size: 13px; font-weight: bold; color: var(--green-hi); }

/* ── Help tooltips ── */
.help-tip {
  display: inline-flex; align-items: center; justify-content: center;
  width: 13px; height: 13px; border-radius: 50%;
  border: 1px solid var(--gray); color: var(--dim);
  font-size: 9px; cursor: help; margin-left: 4px;
  position: relative; vertical-align: middle;
  flex-shrink: 0; letter-spacing: 0; font-family: var(--mono);
  transition: border-color 0.12s, color 0.12s;
}
.help-tip:hover { border-color: var(--blue); color: var(--blue); }
.help-tip::after {
  content: attr(data-tip);
  position: absolute;
  bottom: calc(100% + 7px);
  left: 50%;
  transform: translateX(-50%);
  background: var(--surface2);
  border: 1px solid var(--border);
  color: var(--text);
  font-size: 11px; line-height: 1.55;
  padding: 8px 12px;
  width: 240px;
  z-index: 400;
  pointer-events: none;
  visibility: hidden; opacity: 0;
  transition: opacity 0.12s;
  letter-spacing: 0; text-transform: none;
  font-family: var(--mono); font-weight: normal;
  white-space: normal;
}
.help-tip:hover::after { visibility: visible; opacity: 1; }

/* ── Sky mask ── */
.sky-canvas-ring {
  border: 1px solid var(--border);
  border-radius: 50%;
  overflow: hidden;
  width: 360px; height: 360px;
  flex-shrink: 0;
  display: block;
}
.sky-canvas-ring canvas {
  display: block; cursor: default;
}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <div>
    <div class="hdr-logo">NODE v1</div>
    <div class="hdr-sub">ALPACA CONTROL</div>
  </div>

  <div class="conn-pill-wrap" id="connPillWrap">
    <div class="conn-pill" id="connPill">
      <span class="dot dot-gray" id="connDot"></span>
      <span id="connLabel">Disconnected</span>
    </div>
    <div class="conn-dropdown" id="connDropdown">
      <button class="btn btn-red" onclick="doDisconnect()">Disconnect</button>
    </div>
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
    <button class="btn btn-dim" id="btnConfig" onclick="openConfigModal()">Config</button>
    <button class="btn btn-blue" id="btnDiscover" onclick="showDiscover()">Discover</button>
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

    <!-- Object Catalog Search -->
    <div class="ctrl-group">
      <div class="panel-label">Object Catalog</div>
      <div style="position:relative;">
        <input class="inp" id="catalogSearch" type="text" placeholder="Search M42, Andromeda, nebula…"
          autocomplete="off" spellcheck="false"
          oninput="catalogFilter()" onfocus="catalogFilter()" onkeydown="catalogKeyNav(event)">
        <div id="catalogDropdown" style="display:none;position:absolute;left:0;right:0;top:100%;z-index:200;
          background:var(--panel-bg);border:1px solid var(--border);border-radius:6px;
          max-height:200px;overflow-y:auto;margin-top:2px;box-shadow:0 4px 16px rgba(0,0,0,0.5);"></div>
      </div>
    </div>

    <!-- Slew -->
    <div class="ctrl-group">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
        <div class="panel-label" style="margin:0;">Slew Target</div>
        <div style="display:flex;gap:0;border:1px solid var(--border);border-radius:6px;overflow:hidden;">
          <button id="slewModeEQ" onclick="setSlewMode('eq')"
            style="padding:3px 10px;font-size:10px;letter-spacing:1px;border:none;cursor:pointer;background:var(--blue);color:#fff;">EQ</button>
          <button id="slewModeAltAz" onclick="setSlewMode('altaz')"
            style="padding:3px 10px;font-size:10px;letter-spacing:1px;border:none;cursor:pointer;background:var(--panel-bg);color:var(--dim);">ALT-AZ</button>
        </div>
      </div>
      <div id="slewInputsEQ" class="inp-grid">
        <div class="inp-group">
          <div class="inp-label">R.A. (decimal hours)</div>
          <input class="inp" id="slewRA" type="number" min="0" max="23.9999" step="0.0001" placeholder="0.0000">
        </div>
        <div class="inp-group">
          <div class="inp-label">Dec (decimal degrees)</div>
          <input class="inp" id="slewDec" type="number" min="-90" max="90" step="0.0001" placeholder="0.0000">
        </div>
      </div>
      <div id="slewInputsAltAz" class="inp-grid" style="display:none;">
        <div class="inp-group">
          <div class="inp-label">Altitude (degrees, 0–90)</div>
          <input class="inp" id="slewAlt" type="number" min="0" max="90" step="0.0001" placeholder="0.0000">
        </div>
        <div class="inp-group">
          <div class="inp-label">Azimuth (degrees, 0–360)</div>
          <input class="inp" id="slewAz" type="number" min="0" max="359.9999" step="0.0001" placeholder="0.0000">
        </div>
      </div>
      <button class="btn btn-blue btn-full" id="btnModalSlew" onclick="apiSlew()" disabled>Slew to Target</button>
    </div>

    <!-- Joystick -->
    <div class="ctrl-group">
      <div class="panel-label">Nudge</div>
      <div style="display:flex;flex-direction:column;gap:10px;">
        <div style="display:flex;flex-direction:column;gap:4px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-size:10px;color:var(--dim);letter-spacing:1px;">SPEED</span>
            <span id="joySpeedLabel" style="font-size:10px;color:var(--blue);">1×</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px;">
            <span style="font-size:9px;color:var(--dim);">Fine</span>
            <input id="joySpeed" type="range" min="-2" max="2" step="0.1" value="0"
              style="flex:1;accent-color:var(--blue);cursor:pointer;"
              title="Speed multiplier (logarithmic)">
            <span style="font-size:9px;color:var(--dim);">Fast</span>
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:16px; flex-wrap:wrap;">
          <div id="joyPad" style="width:120px; height:120px; border-radius:50%; background:var(--panel-bg); border:2px solid var(--border); position:relative; cursor:grab; touch-action:none; user-select:none; flex-shrink:0;" title="Hold and drag to move — distance sets speed">
            <span style="position:absolute;top:4px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--dim);pointer-events:none;">N</span>
            <span style="position:absolute;bottom:4px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--dim);pointer-events:none;">S</span>
            <span style="position:absolute;left:5px;top:50%;transform:translateY(-50%);font-size:9px;color:var(--dim);pointer-events:none;">W</span>
            <span style="position:absolute;right:5px;top:50%;transform:translateY(-50%);font-size:9px;color:var(--dim);pointer-events:none;">E</span>
            <div id="joyKnob" style="width:34px;height:34px;border-radius:50%;background:var(--blue);position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);pointer-events:none;transition:background 0.1s;box-shadow:0 0 8px rgba(96,165,250,0.4);"></div>
          </div>
          <div style="display:flex;flex-direction:column;gap:4px;">
            <div id="joyReadout" style="font-size:11px;color:var(--dim);">drag to nudge</div>
            <div id="joyDir"     style="font-size:13px;color:var(--blue);min-height:18px;"></div>
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

<!-- Config editor modal -->
<div class="modal hidden cfg-modal" id="cfgModal" onclick="if(event.target===this)closeConfigModal()">
  <div class="modal-content" style="max-width:780px;width:95%;max-height:90vh;height:90vh;">
    <div class="modal-header" style="flex-shrink:0;">
      <div class="modal-title" style="font-size:14px;">⚙ Configuration</div>
      <div style="display:flex;gap:8px;align-items:center;">
        <button class="btn btn-dim" id="cfgViewToggle" onclick="toggleCfgView()" style="font-size:10px;padding:3px 10px;letter-spacing:1px;">RAW YAML</button>
        <button class="modal-close" onclick="closeConfigModal()">×</button>
      </div>
    </div>

    <!-- Tab bar -->
    <div id="cfgTabs" style="display:flex;border-bottom:1px solid var(--border);flex-shrink:0;overflow-x:auto;">
      <button id="cfgTab_setup"      class="cfg-tab active" onclick="switchCfgTab('setup')">Setup</button>
      <button id="cfgTab_photometry" class="cfg-tab" onclick="switchCfgTab('photometry')">Photometry</button>
      <button id="cfgTab_aavso"      class="cfg-tab" onclick="switchCfgTab('aavso')">AAVSO</button>
      <button id="cfgTab_safety"     class="cfg-tab" onclick="switchCfgTab('safety')">Safety</button>
      <button id="cfgTab_advanced"   class="cfg-tab" onclick="switchCfgTab('advanced')">Advanced</button>
    </div>

    <!-- Form view -->
    <div id="cfgFormView" style="flex:1;overflow-y:auto;min-height:0;">

      <!-- SETUP -->
      <div id="cfgPanel_setup" class="cfg-panel">
        <div class="cfg-section-hdr">Observer Location</div>
        <div style="font-size:11px;color:var(--dim);margin-bottom:6px;">Your site coordinates — used for Alt-Az slewing, sun elevation, and dawn time calculations.</div>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">Latitude (° N) <span class="help-tip" data-tip="Decimal degrees north of the equator. North is positive, South is negative. Example: 51.5074 for London, -33.8688 for Sydney.">?</span></div>
            <input class="inp" type="number" id="cfgObsLat" min="-90" max="90" step="0.0001" placeholder="e.g. 51.5074">
          </div>
          <div class="inp-group">
            <div class="inp-label">Longitude (° E) <span class="help-tip" data-tip="Decimal degrees east of Greenwich. West is negative. Example: -0.1278 for London, 151.2093 for Sydney.">?</span></div>
            <input class="inp" type="number" id="cfgObsLon" min="-180" max="180" step="0.0001" placeholder="e.g. -0.1278">
          </div>
        </div>
        <div class="cfg-section-hdr">Devices</div>
        <div style="font-size:11px;color:var(--dim);margin-bottom:6px;">Which ALPACA devices to connect when you hit Discover/Connect. Device numbers are per-server indices — almost always 0.</div>
        <div class="cfg-device-row">
          <label class="cfg-toggle" style="flex:1;">
            <input type="checkbox" id="cfgDevTelEnabled">
            <span>Telescope</span>
            <span class="help-tip" data-tip="Enable ALPACA telescope control. Required for slewing, tracking, parking, and nudging.">?</span>
          </label>
          <div class="inp-group" style="max-width:110px;">
            <div class="inp-label">Device # <span class="help-tip" data-tip="ALPACA device index on the server. Almost always 0 unless multiple mounts are connected to the same server.">?</span></div>
            <input class="inp" type="number" id="cfgDevTelNum" min="0" max="99" step="1">
          </div>
        </div>
        <div class="cfg-device-row">
          <label class="cfg-toggle" style="flex:1;">
            <input type="checkbox" id="cfgDevCamEnabled">
            <span>Camera</span>
            <span class="help-tip" data-tip="Enable ALPACA camera control. Required for taking exposures through the dashboard.">?</span>
          </label>
          <div class="inp-group" style="max-width:110px;">
            <div class="inp-label">Device # <span class="help-tip" data-tip="ALPACA device index for the camera. Almost always 0.">?</span></div>
            <input class="inp" type="number" id="cfgDevCamNum" min="0" max="99" step="1">
          </div>
        </div>
        <div class="cfg-device-row" style="opacity:0.55;">
          <label class="cfg-toggle" style="flex:1;">
            <input type="checkbox" id="cfgDevFocEnabled">
            <span>Focuser</span>
            <span class="help-tip" data-tip="Enable ALPACA focuser. Not yet exposed in the dashboard UI — connecting it here enables future support.">?</span>
          </label>
          <div class="inp-group" style="max-width:110px;">
            <div class="inp-label">Device #</div>
            <input class="inp" type="number" id="cfgDevFocNum" min="0" max="99" step="1">
          </div>
        </div>
        <div class="cfg-device-row" style="opacity:0.55;">
          <label class="cfg-toggle" style="flex:1;">
            <input type="checkbox" id="cfgDevFwEnabled">
            <span>Filter Wheel</span>
            <span class="help-tip" data-tip="Enable ALPACA filter wheel. Not yet exposed in the dashboard UI — connecting it here enables future support.">?</span>
          </label>
          <div class="inp-group" style="max-width:110px;">
            <div class="inp-label">Device #</div>
            <input class="inp" type="number" id="cfgDevFwNum" min="0" max="99" step="1">
          </div>
        </div>
      </div>

      <!-- PHOTOMETRY -->
      <div id="cfgPanel_photometry" class="cfg-panel" style="display:none;">
        <label class="cfg-toggle cfg-toggle-lg">
          <input type="checkbox" id="cfgPhotEnabled">
          <span>Photometry Pipeline Enabled</span>
          <span class="help-tip" data-tip="Automatically run aperture photometry on each new FITS file detected by the image watcher. Requires the image watcher to be configured in Advanced.">?</span>
        </label>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">Node ID <span class="help-tip" data-tip="Unique name for this observing node in the Boundless Skies network. Used to label your data contributions. Example: node_001.">?</span></div>
            <input class="inp" type="text" id="cfgPhotNodeId" placeholder="node_001">
          </div>
          <div class="inp-group">
            <div class="inp-label">Filter (AAVSO code) <span class="help-tip" data-tip="AAVSO filter code for your instrument. CV = Clear/Visual broadband (Seestar S50 default), V = Johnson-V, B = Johnson-B, R = Cousins-R.">?</span></div>
            <input class="inp" type="text" id="cfgPhotFilter" placeholder="CV">
          </div>
        </div>
        <div class="cfg-section-hdr">Target Override</div>
        <div style="font-size:11px;color:var(--dim);margin-bottom:4px;">Leave all blank to use coordinates from the FITS header — the normal mode when your Seestar is scheduled to a target.</div>
        <div class="cfg-field-grid" style="grid-template-columns:2fr 1fr 1fr;">
          <div class="inp-group">
            <div class="inp-label">Target Name <span class="help-tip" data-tip="Override the star name in submissions. Leave blank to use the OBJECT field from your FITS header.">?</span></div>
            <input class="inp" type="text" id="cfgPhotTargetName" placeholder="e.g. SS Cyg">
          </div>
          <div class="inp-group">
            <div class="inp-label">RA (° decimal) <span class="help-tip" data-tip="Override the target right ascension in decimal degrees (0-360). Leave blank to use WCS from the FITS header.">?</span></div>
            <input class="inp" type="number" id="cfgPhotTargetRA" step="0.0001" placeholder="null">
          </div>
          <div class="inp-group">
            <div class="inp-label">Dec (° decimal) <span class="help-tip" data-tip="Override the target declination in decimal degrees (-90 to +90). Leave blank to use WCS from the FITS header.">?</span></div>
            <input class="inp" type="number" id="cfgPhotTargetDec" step="0.0001" placeholder="null">
          </div>
        </div>
        <div class="cfg-section-hdr">Plate Solving</div>
        <div style="font-size:11px;color:var(--dim);margin-bottom:4px;">Only runs when a FITS file has no WCS solution. The Seestar S50 usually includes WCS.</div>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">ASTAP Path <span class="help-tip" data-tip="Path to the ASTAP executable. Use 'astap' if it's on your system PATH, or the full path e.g. /usr/local/bin/astap.">?</span></div>
            <input class="inp" type="text" id="cfgPhotAstap" placeholder="astap">
          </div>
          <div class="inp-group">
            <div class="inp-label">Search Radius (°) <span class="help-tip" data-tip="How far from the expected position ASTAP will search. Larger is slower but more forgiving of pointing errors.">?</span></div>
            <input class="inp" type="number" id="cfgPhotAstapRadius" min="1" max="90" step="1">
          </div>
        </div>
        <div class="cfg-section-hdr">Aperture Geometry <span style="font-size:9px;color:var(--dim);letter-spacing:0;text-transform:none;">(multiples of FWHM)</span></div>
        <div class="cfg-field-grid" style="grid-template-columns:1fr 1fr 1fr;">
          <div class="inp-group">
            <div class="inp-label">Aperture Radius <span class="help-tip" data-tip="Photometric aperture radius as a multiple of the stellar FWHM. 2.5x captures ~99% of a star's light with a well-focused PSF. Increase if seeing is poor.">?</span></div>
            <input class="inp" type="number" id="cfgPhotAperture" min="0.5" max="10" step="0.1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Sky Annulus Inner <span class="help-tip" data-tip="Inner edge of the sky background annulus in multiples of FWHM. Must be large enough to exclude the star's PSF wings.">?</span></div>
            <input class="inp" type="number" id="cfgPhotAnnulusIn" min="1" max="20" step="0.1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Sky Annulus Outer <span class="help-tip" data-tip="Outer edge of the sky background annulus. Larger gives a better background estimate but more contamination from nearby stars.">?</span></div>
            <input class="inp" type="number" id="cfgPhotAnnulusOut" min="2" max="30" step="0.1">
          </div>
        </div>
        <div class="cfg-section-hdr">Comparison Stars &amp; Quality</div>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">Field Radius (°) <span class="help-tip" data-tip="Half-width of the region to search for AAVSO comparison stars. Should match your FOV — the Seestar S50 is about 0.5 degrees on the short axis.">?</span></div>
            <input class="inp" type="number" id="cfgPhotFieldRadius" min="0.1" max="5" step="0.1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Magnitude Limit <span class="help-tip" data-tip="Faintest comparison stars to retrieve from AAVSO. Stars fainter than this are excluded. 15.0 mag is suitable for the Seestar S50.">?</span></div>
            <input class="inp" type="number" id="cfgPhotMagLimit" min="8" max="20" step="0.5">
          </div>
          <div class="inp-group">
            <div class="inp-label">Min Comparison Stars <span class="help-tip" data-tip="Minimum comparison stars required for a valid observation. Below this count the observation is flagged poor quality.">?</span></div>
            <input class="inp" type="number" id="cfgPhotMinComp" min="1" max="20" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Min SNR <span class="help-tip" data-tip="Minimum signal-to-noise ratio for the target star. Observations below this are flagged poor quality.">?</span></div>
            <input class="inp" type="number" id="cfgPhotSNR" min="5" max="200" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Max Uncertainty (mag) <span class="help-tip" data-tip="Maximum magnitude uncertainty for a good-quality result. Measurements with higher uncertainty are flagged poor.">?</span></div>
            <input class="inp" type="number" id="cfgPhotMaxUnc" min="0.01" max="1" step="0.01">
          </div>
          <div class="inp-group">
            <div class="inp-label">Max Airmass <span class="help-tip" data-tip="Maximum airmass (atmospheric path length) for a valid observation. High airmass = low altitude = more atmospheric distortion. 3.0 is the standard AAVSO limit.">?</span></div>
            <input class="inp" type="number" id="cfgPhotMaxAirmass" min="1" max="5" step="0.1">
          </div>
        </div>
      </div>

      <!-- AAVSO -->
      <div id="cfgPanel_aavso" class="cfg-panel" style="display:none;">
        <div style="font-size:11px;color:var(--dim);margin-bottom:8px;">Your AAVSO account credentials for submitting photometry to the International Variable Star Database.</div>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">Observer Code (OBSCODE) <span class="help-tip" data-tip="Your AAVSO observer code — 4 to 7 capital letters, found in your AAVSO account settings. Required. Embedded in every observation to link it to your account.">?</span></div>
            <input class="inp" type="text" id="cfgAavsoCode" placeholder="MXYZ" style="text-transform:uppercase;">
          </div>
          <div class="inp-group">
            <div class="inp-label">Username <span class="help-tip" data-tip="Your AAVSO website login username or email. Required to POST observations. Stored in plain text in config.yaml — keep this file private.">?</span></div>
            <input class="inp" type="text" id="cfgAavsoUser" autocomplete="username">
          </div>
          <div class="inp-group" style="grid-column:1/-1;">
            <div class="inp-label">Password <span class="help-tip" data-tip="Your AAVSO website login password. Stored in plain text in config.yaml — keep this file private and do not commit it to version control.">?</span></div>
            <input class="inp" type="password" id="cfgAavsoPass" autocomplete="current-password">
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:10px;margin-top:4px;">
          <label class="cfg-toggle">
            <input type="checkbox" id="cfgAavsosDryRun">
            <span>Dry run — format &amp; save locally but do not submit to AAVSO</span>
            <span class="help-tip" data-tip="Test mode: the pipeline runs fully and saves formatted observation files, but nothing is uploaded to AAVSO. Use this to verify your pipeline before going live.">?</span>
          </label>
          <label class="cfg-toggle">
            <input type="checkbox" id="cfgAavsoSubmitPoor">
            <span>Submit poor-quality observations</span>
            <span class="help-tip" data-tip="Allow uploading observations flagged poor quality (low SNR, high uncertainty, or high airmass). Off by default — AAVSO prefers high-quality data.">?</span>
          </label>
        </div>
        <div class="cfg-section-hdr">Additional Settings</div>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">Chart ID <span class="help-tip" data-tip="The AAVSO Variable Star Plotter (VSP) chart ID for your target. Leave blank for 'na'. Find chart IDs on the AAVSO website under Variable Star Plotter.">?</span></div>
            <input class="inp" type="text" id="cfgAavsoChartId" placeholder="X26297EX (or leave blank)">
          </div>
          <div class="inp-group">
            <div class="inp-label">Audit Directory <span class="help-tip" data-tip="Local directory where formatted observation files and submission records are saved. Created automatically if it does not exist.">?</span></div>
            <input class="inp" type="text" id="cfgAavsoAuditDir" placeholder="aavso_submissions">
          </div>
        </div>
      </div>

      <!-- SAFETY -->
      <div id="cfgPanel_safety" class="cfg-panel" style="display:none;">
        <label class="cfg-toggle cfg-toggle-lg">
          <input type="checkbox" id="cfgSafetyEnabled">
          <span>Safety Manager Enabled</span>
          <span class="help-tip" data-tip="Enable the safety manager. Monitors telescope connection health, parks at dawn, and enforces the horizon mask. Only disable for manual testing.">?</span>
        </label>
        <div class="cfg-section-hdr">Dawn Protection</div>
        <label class="cfg-toggle">
          <input type="checkbox" id="cfgSafetyParkDawn">
          <span>Auto-park at dawn</span>
          <span class="help-tip" data-tip="Automatically park the telescope when the sun rises to the dawn threshold. Protects the optics from daytime sun exposure.">?</span>
        </label>
        <div class="cfg-field-grid" style="grid-template-columns:1fr;">
          <div class="inp-group">
            <div class="inp-label">Dawn Type <span class="help-tip" data-tip="Which definition of dawn triggers the park. Astronomical (-18 deg) is the latest and darkest — standard for deep-sky. Nautical (-12) and civil (-6) park progressively earlier.">?</span></div>
            <select class="inp" id="cfgSafetyDawnType">
              <option value="astronomical">Astronomical (−18° — standard for deep-sky)</option>
              <option value="nautical">Nautical (−12°)</option>
              <option value="civil">Civil (−6°)</option>
            </select>
          </div>
        </div>
        <div class="cfg-section-hdr">Connection Watchdog</div>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">Heartbeat Interval (sec) <span class="help-tip" data-tip="How often the safety manager checks that the telescope is still responding. Lower values catch disconnects faster but add a little network traffic.">?</span></div>
            <input class="inp" type="number" id="cfgSafetyHb" min="5" max="300" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Disconnect Timeout (sec) <span class="help-tip" data-tip="If the telescope is unreachable for this many seconds, the safety manager parks and flags an error. Set high enough to survive brief network glitches.">?</span></div>
            <input class="inp" type="number" id="cfgSafetyDiscoTo" min="30" max="3600" step="30">
          </div>
          <div class="inp-group">
            <div class="inp-label">Reconnect Attempts <span class="help-tip" data-tip="How many times to retry a failed connection check before declaring the telescope lost and triggering a park.">?</span></div>
            <input class="inp" type="number" id="cfgSafetyReconAttempts" min="0" max="20" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Reconnect Delay (sec) <span class="help-tip" data-tip="Seconds to wait between connection retry attempts.">?</span></div>
            <input class="inp" type="number" id="cfgSafetyReconDelay" min="1" max="120" step="1">
          </div>
        </div>
        <div class="cfg-section-hdr">Horizon Mask</div>
        <div style="font-size:11px;color:var(--dim);margin-bottom:8px;">
          Drag each handle radially to set the minimum safe altitude in that direction.
          Or use <strong style="color:var(--blue);">Auto-Scan</strong> to detect your horizon automatically.
        </div>
        <div style="display:flex;flex-direction:column;align-items:center;gap:8px;">
          <div class="sky-canvas-ring">
            <canvas id="skyCanvas" width="360" height="360"></canvas>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;width:360px;min-height:18px;">
            <div id="skyCoordInfo" style="font-size:12px;color:var(--blue);font-family:var(--mono);"></div>
            <div id="skyHint"      style="font-size:10px;color:var(--dim);letter-spacing:1px;text-align:right;"></div>
          </div>
          <div style="display:flex;gap:8px;width:360px;">
            <button class="btn btn-dim"   onclick="clearSkyMask()" style="flex:1;">Reset</button>
            <button class="btn btn-green" id="btnSkyMaskSave" onclick="saveSkyMask()" style="flex:2;">Save Mask</button>
          </div>

          <!-- ── Auto-Scan ─────────────────────────────────────────────────── -->
          <div style="width:360px;border-top:1px solid var(--border);padding-top:10px;margin-top:2px;">
            <div style="font-size:11px;font-weight:600;color:var(--blue);margin-bottom:8px;letter-spacing:.5px;">AUTO-SCAN HORIZON</div>
            <div style="font-size:10px;color:var(--dim);margin-bottom:8px;line-height:1.5;">
              Slews the telescope to 12 azimuths, stepping down from <em>Start Alt</em> to the hardware
              <em>Floor</em>. Star count at each step determines your obstruction profile.
              Run at night with a clear sky. Takes ~15–40 min.
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:8px;" id="scanConfigFields">
              <div class="inp-group">
                <div class="inp-label">Floor Alt °</div>
                <input class="inp" id="scanFloorAlt" type="number" value="25" min="5" max="45" step="5">
              </div>
              <div class="inp-group">
                <div class="inp-label">Start Alt °</div>
                <input class="inp" id="scanStartAlt" type="number" value="60" min="30" max="85" step="5">
              </div>
              <div class="inp-group">
                <div class="inp-label">Step °</div>
                <input class="inp" id="scanStep" type="number" value="5" min="2" max="15" step="1">
              </div>
              <div class="inp-group">
                <div class="inp-label">Exposure s</div>
                <input class="inp" id="scanExposure" type="number" value="5" min="1" max="60" step="1">
              </div>
              <div class="inp-group">
                <div class="inp-label">Min Stars</div>
                <input class="inp" id="scanStarThresh" type="number" value="5" min="1" max="100" step="1">
              </div>
              <div class="inp-group">
                <div class="inp-label">Settle s</div>
                <input class="inp" id="scanSettle" type="number" value="2" min="0" max="15" step="1">
              </div>
            </div>

            <!-- Progress strip (hidden when idle) -->
            <div id="scanProgress" style="display:none;margin-bottom:8px;">
              <div id="scanProgressLabel" style="font-size:10px;color:var(--dim);margin-bottom:6px;font-family:var(--mono);"></div>
              <div id="scanDirectionBars" style="display:flex;gap:2px;height:20px;align-items:flex-end;"></div>
            </div>

            <div style="display:flex;gap:8px;">
              <button id="btnStartScan"  class="btn" style="flex:1;background:var(--blue);color:#fff;"
                      onclick="startHorizonScan()">Scan Horizon</button>
              <button id="btnCancelScan" class="btn btn-red"   style="flex:1;display:none;"
                      onclick="cancelHorizonScan()">Cancel</button>
              <button id="btnApplyScan"  class="btn btn-green" style="flex:1;display:none;"
                      onclick="applyHorizonScanResult()">Apply Result</button>
            </div>
          </div>
          <!-- ── /Auto-Scan ──────────────────────────────────────────────── -->
        </div>
      </div>

      <!-- ADVANCED -->
      <div id="cfgPanel_advanced" class="cfg-panel" style="display:none;">
        <div class="cfg-section-hdr">ALPACA Discovery</div>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">Discovery Port <span class="help-tip" data-tip="UDP broadcast port for ALPACA server discovery. The ALPACA spec defines 32227. Only change if your server uses a non-standard port.">?</span></div>
            <input class="inp" type="number" id="cfgAlpacaPort" min="1024" max="65535" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Timeout (sec) <span class="help-tip" data-tip="How long to wait for ALPACA servers to respond during a LAN scan. Increase for slow or congested networks.">?</span></div>
            <input class="inp" type="number" id="cfgAlpacaTimeout" min="1" max="60" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">API Version <span class="help-tip" data-tip="ALPACA API version to use. Almost always 1. Only change if your server requires a different version.">?</span></div>
            <input class="inp" type="number" id="cfgAlpacaApiVer" min="1" max="2" step="1">
          </div>
        </div>
        <div class="cfg-section-hdr">Device Defaults</div>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">Tracking Rate <span class="help-tip" data-tip="Default tracking rate when the telescope connects. 0 = Sidereal (for stars — the usual choice), 1 = Lunar, 2 = Solar, 3 = King rate (corrects for refraction).">?</span></div>
            <select class="inp" id="cfgTrackingRate">
              <option value="0">0 — Sidereal (stars)</option>
              <option value="1">1 — Lunar</option>
              <option value="2">2 — Solar</option>
              <option value="3">3 — King rate</option>
            </select>
          </div>
          <div class="inp-group">
            <div class="inp-label">Camera Exposure (sec) <span class="help-tip" data-tip="Default exposure duration pre-filled in the Camera panel. You can override it per-exposure.">?</span></div>
            <input class="inp" type="number" id="cfgCamExposure" min="0.001" step="0.1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Camera Binning <span class="help-tip" data-tip="Default binning pre-filled in the Camera panel. 1 = full resolution. 2 = 2x2 binning (4x faster readout, lower resolution).">?</span></div>
            <input class="inp" type="number" id="cfgCamBinning" min="1" max="8" step="1">
          </div>
        </div>
        <div class="cfg-section-hdr">Pier Camera (ZWO Live View)</div>
        <label class="cfg-toggle" style="margin-bottom:6px;">
          <input type="checkbox" id="cfgPierEnabled">
          <span>Enabled</span>
          <span class="help-tip" data-tip="Enable live video preview from a ZWO ASI camera at the pier. Requires the zwoasi Python package and the ZWO ASI SDK library.">?</span>
        </label>
        <div class="cfg-field-grid" style="grid-template-columns:1fr 1fr 1fr;">
          <div class="inp-group">
            <div class="inp-label">Device Index <span class="help-tip" data-tip="ZWO SDK camera index (not the ALPACA device number). Usually 0 for the first connected ZWO camera.">?</span></div>
            <input class="inp" type="number" id="cfgPierDevIdx" min="0" max="9" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Exposure (ms) <span class="help-tip" data-tip="Exposure time per preview frame in milliseconds. Shorter = faster frame rate but noisier in low light.">?</span></div>
            <input class="inp" type="number" id="cfgPierExpMs" min="1" max="30000" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Gain <span class="help-tip" data-tip="Camera gain (0-600 for most ZWO cameras). Higher = more sensitive but more noise.">?</span></div>
            <input class="inp" type="number" id="cfgPierGain" min="0" max="600" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Bin <span class="help-tip" data-tip="Binning factor for live preview. 2 = half resolution, 4x faster — recommended for most setups.">?</span></div>
            <input class="inp" type="number" id="cfgPierBin" min="1" max="4" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">Target FPS <span class="help-tip" data-tip="Maximum preview frames per second. Capped by exposure time and USB bandwidth.">?</span></div>
            <input class="inp" type="number" id="cfgPierFps" min="1" max="60" step="1">
          </div>
          <div class="inp-group">
            <div class="inp-label">JPEG Quality <span class="help-tip" data-tip="JPEG compression quality for preview frames (10-100). Lower = smaller and faster streaming. Higher = better image quality.">?</span></div>
            <input class="inp" type="number" id="cfgPierJpegQ" min="10" max="100" step="5">
          </div>
        </div>
        <div class="inp-group">
          <div class="inp-label">SDK Library Path <span class="help-tip" data-tip="Full path to the ZWO ASI SDK shared library (libASICamera2.so or .dylib). Leave blank to auto-detect. Example: /usr/lib/libASICamera2.so">?</span></div>
          <input class="inp" type="text" id="cfgPierSdkLib" placeholder="Leave blank to auto-detect">
        </div>
        <div class="cfg-section-hdr">Image Watcher</div>
        <label class="cfg-toggle" style="margin-bottom:6px;">
          <input type="checkbox" id="cfgIwEnabled">
          <span>Enabled — watch for incoming FITS files</span>
          <span class="help-tip" data-tip="Monitor a directory for new FITS files. When a new file appears it is displayed in the dashboard and optionally processed by the photometry pipeline.">?</span>
        </label>
        <div class="cfg-field-grid">
          <div class="inp-group" style="grid-column:1/-1;">
            <div class="inp-label">Watch Path <span class="help-tip" data-tip="Directory to monitor. For a Seestar on your network, this is the mount point of its SMB share. For local imaging software, this is its output directory.">?</span></div>
            <input class="inp" type="text" id="cfgIwPath" placeholder="/mnt/seestar">
          </div>
          <div class="inp-group">
            <div class="inp-label">Debounce Delay (sec) <span class="help-tip" data-tip="Seconds to wait after a file appears before processing it. Prevents reading incomplete files still being written. 2 seconds is safe for SMB network shares.">?</span></div>
            <input class="inp" type="number" id="cfgIwDebounce" min="0.1" max="30" step="0.1">
          </div>
        </div>
        <div class="cfg-section-hdr">Logging</div>
        <div class="cfg-field-grid" style="grid-template-columns:1fr 2fr;">
          <div class="inp-group">
            <div class="inp-label">Log Level <span class="help-tip" data-tip="Minimum severity to log. DEBUG is verbose. INFO is recommended for normal use. WARNING and ERROR suppress routine messages.">?</span></div>
            <select class="inp" id="cfgLogLevel">
              <option value="DEBUG">DEBUG</option>
              <option value="INFO">INFO</option>
              <option value="WARNING">WARNING</option>
              <option value="ERROR">ERROR</option>
            </select>
          </div>
        </div>
      </div>

    </div><!-- /cfgFormView -->

    <!-- Raw YAML view -->
    <textarea class="cfg-textarea" id="cfgTextarea" spellcheck="false" style="display:none;"></textarea>

    <div class="cfg-error" id="cfgError"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;flex-shrink:0;">
      <button class="btn btn-dim" onclick="closeConfigModal()">Cancel</button>
      <button class="btn btn-green" id="btnCfgSave" onclick="saveConfig()">Save</button>
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
<div class="overlay hidden" id="overlay" onclick="if(event.target===this)hideDiscover()">
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

let _joyBlocked = true;

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
  renderTelescope(s.telescope || {});
  renderCamera(s.camera || {});
  renderSafety(s.safety || {});
  renderImage(s);
  renderPierCam(s.pier_cam || {});
}

// ── Connection dropdown ──────────────────────────────────────────────────────

function toggleConnDropdown(e) {
  e.stopPropagation();
  document.getElementById("connDropdown").classList.toggle("open");
}

document.addEventListener("click", function() {
  document.getElementById("connDropdown").classList.remove("open");
});

async function doDisconnect() {
  document.getElementById("connDropdown").classList.remove("open");
  await fetch("/api/disconnect", { method: "POST" });
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
    closeConfigModal();
    hideDiscover();
  }
});

// ── Header ──────────────────────────────────────────────────────────────────

function renderHeader(s) {
  const dot   = document.getElementById("connDot");
  const label = document.getElementById("connLabel");
  const pill  = document.getElementById("connPill");

  const srv = document.getElementById("hdrServer");
  if (s.server) {
    srv.classList.remove("hidden");
    document.getElementById("hdrAddr").textContent =
      `${s.server.address}:${s.server.port}`;
  } else {
    srv.classList.add("hidden");
  }

  if (s.connected) {
    dot.className    = "dot dot-green";
    label.textContent = "Connected";
    pill.classList.add("clickable");
    pill.onclick = toggleConnDropdown;
  } else {
    dot.className    = "dot dot-gray";
    label.textContent = "Disconnected";
    pill.classList.remove("clickable");
    pill.onclick = null;
    document.getElementById("connDropdown").classList.remove("open");
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
  _joyBlocked = blocked;
  const pad = document.getElementById("joyPad");
  if (pad) {
    pad.style.opacity = blocked ? "0.35" : "1";
    pad.style.cursor  = blocked ? "not-allowed" : "grab";
  }
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

// ── Joystick ─────────────────────────────────────────────────────────────────

(function () {
  const PAD_R      = 60;          // outer radius px
  const KNOB_R     = 17;          // knob radius px
  const MAX_R      = PAD_R - KNOB_R;  // max knob travel (43 px)
  const DEAD_ZONE  = 6;           // px — ignore sub-pixel jitter
  const BASE_RATE  = 60 / 3600;   // deg/s at full deflection with speed=1× (1 arcmin/s)
  const SEND_MS    = 80;          // rate-update interval while held

  let active = false;
  let originX = 0, originY = 0;
  let curDx = 0, curDy = 0;
  let sendTimer = null;
  let pendingSend = false;

  function speedMult() {
    const slider = document.getElementById("joySpeed");
    return slider ? Math.pow(10, parseFloat(slider.value)) : 1;
  }

  function formatRate(degPerSec) {
    const arcsec = degPerSec * 3600;
    if (arcsec < 60)  return arcsec.toFixed(1) + "″/s";
    if (arcsec < 3600) return (arcsec / 60).toFixed(1) + "′/s";
    return (arcsec / 3600).toFixed(2) + "°/s";
  }

  function resetKnob() {
    const knob = document.getElementById("joyKnob");
    if (knob) knob.style.transform = "translate(-50%, -50%)";
    const readout = document.getElementById("joyReadout");
    if (readout) readout.textContent = "drag to move";
    const dir = document.getElementById("joyDir");
    if (dir) dir.textContent = "";
    const pad = document.getElementById("joyPad");
    if (pad && !_joyBlocked) pad.style.cursor = "grab";
  }

  function computeRates(dx, dy) {
    const nx = Math.max(-1, Math.min(1, dx / MAX_R));
    const ny = Math.max(-1, Math.min(1, dy / MAX_R));
    const speed = BASE_RATE * speedMult();
    // Axis 1 (Dec): positive = North; dy negative = up = North → negate
    // Axis 0 (RA): positive = East (RA decreasing); dx positive = right = East
    return { ra_rate: nx * speed, dec_rate: -ny * speed };
  }

  function sendMove() {
    if (!active || !pendingSend) return;
    pendingSend = false;
    const dist = Math.sqrt(curDx * curDx + curDy * curDy);
    if (dist < DEAD_ZONE) {
      apiMoveAxis(0, 0);
      return;
    }
    const { ra_rate, dec_rate } = computeRates(curDx, curDy);
    apiMoveAxis(ra_rate, dec_rate);
  }

  function onStart(e) {
    if (_joyBlocked) return;
    e.preventDefault();
    active = true;
    curDx = 0; curDy = 0; pendingSend = false;
    const pad  = document.getElementById("joyPad");
    const rect = pad.getBoundingClientRect();
    originX = rect.left + rect.width  / 2;
    originY = rect.top  + rect.height / 2;
    pad.style.cursor = "grabbing";
    pad.setPointerCapture(e.pointerId);
    sendTimer = setInterval(sendMove, SEND_MS);
  }

  function onMove(e) {
    if (!active) return;
    e.preventDefault();
    curDx = e.clientX - originX;
    curDy = e.clientY - originY;
    pendingSend = true;

    const dist  = Math.sqrt(curDx * curDx + curDy * curDy);
    const clamp = Math.min(dist, MAX_R);
    const scale = clamp / (dist || 1);

    const knob = document.getElementById("joyKnob");
    if (knob) knob.style.transform = `translate(calc(-50% + ${curDx * scale}px), calc(-50% + ${curDy * scale}px))`;

    const { ra_rate, dec_rate } = computeRates(curDx, curDy);
    const totalRate = Math.sqrt(ra_rate * ra_rate + dec_rate * dec_rate);
    const readout = document.getElementById("joyReadout");
    if (readout) readout.textContent = dist < DEAD_ZONE ? "drag to move" : formatRate(totalRate);

    const dirEl = document.getElementById("joyDir");
    if (dirEl && dist >= DEAD_ZONE) {
      const ns = dec_rate > 0 ? "N" : dec_rate < 0 ? "S" : "";
      const ew = ra_rate  > 0 ? "E" : ra_rate  < 0 ? "W" : "";
      const arrows = { N:"↑", S:"↓", E:"→", W:"←" };
      const labels = { N:"North", S:"South", E:"East", W:"West" };
      if (ns && ew) dirEl.textContent = `${arrows[ns]}${arrows[ew]} ${labels[ns]}-${labels[ew]}`;
      else if (ns)  dirEl.textContent = `${arrows[ns]} ${labels[ns]}`;
      else if (ew)  dirEl.textContent = `${arrows[ew]} ${labels[ew]}`;
    } else if (dirEl) {
      dirEl.textContent = "";
    }
  }

  function stopAll() {
    if (sendTimer) { clearInterval(sendTimer); sendTimer = null; }
    active = false;
    apiMoveAxis(0, 0);
    resetKnob();
  }

  function onEnd() {
    if (!active) return;
    stopAll();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const pad = document.getElementById("joyPad");
    if (!pad) return;
    pad.addEventListener("pointerdown", onStart);
    pad.addEventListener("pointermove", onMove);
    pad.addEventListener("pointerup",   onEnd);
    pad.addEventListener("pointercancel", stopAll);

    const slider = document.getElementById("joySpeed");
    const label  = document.getElementById("joySpeedLabel");
    if (slider && label) {
      function updateSpeedLabel() {
        const m = Math.pow(10, parseFloat(slider.value));
        label.textContent = m < 1 ? (m).toFixed(2) + "×" : m.toFixed(m >= 10 ? 0 : 1) + "×";
      }
      slider.addEventListener("input", updateSpeedLabel);
      updateSpeedLabel();
    }
  });
})();

async function apiNudge(direction, step) {
  try {
    const r = await fetch("/api/telescope/nudge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction, step }),
    });
    const d = await r.json();
    if (!d.ok) alert(d.error || "Nudge failed");
  } catch (e) { alert("Nudge failed: " + e.message); }
}

function apiMoveAxis(ra_rate, dec_rate) {
  fetch("/api/telescope/moveaxis", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ra_rate, dec_rate }),
  }).catch(() => {});
}

// ── Object Catalog ───────────────────────────────────────────────────────────

const MESSIER_CATALOG = [
  {id:"M1",  name:"Crab Nebula",          type:"SNR",               ra:5.5755,  dec:22.015  },
  {id:"M2",  name:"",                      type:"Globular Cluster",  ra:21.5578, dec:-0.823  },
  {id:"M3",  name:"",                      type:"Globular Cluster",  ra:13.7028, dec:28.378  },
  {id:"M4",  name:"",                      type:"Globular Cluster",  ra:16.3928, dec:-26.526 },
  {id:"M5",  name:"",                      type:"Globular Cluster",  ra:15.3094, dec:2.081   },
  {id:"M6",  name:"Butterfly Cluster",     type:"Open Cluster",      ra:17.6694, dec:-32.217 },
  {id:"M7",  name:"Ptolemy Cluster",       type:"Open Cluster",      ra:17.8978, dec:-34.841 },
  {id:"M8",  name:"Lagoon Nebula",         type:"Emission Nebula",   ra:18.0628, dec:-24.384 },
  {id:"M9",  name:"",                      type:"Globular Cluster",  ra:17.3194, dec:-18.516 },
  {id:"M10", name:"",                      type:"Globular Cluster",  ra:16.9528, dec:-4.101  },
  {id:"M11", name:"Wild Duck Cluster",     type:"Open Cluster",      ra:18.8511, dec:-6.271  },
  {id:"M12", name:"",                      type:"Globular Cluster",  ra:16.7872, dec:-1.949  },
  {id:"M13", name:"Hercules Cluster",      type:"Globular Cluster",  ra:16.6944, dec:36.461  },
  {id:"M14", name:"",                      type:"Globular Cluster",  ra:17.6261, dec:-3.247  },
  {id:"M15", name:"",                      type:"Globular Cluster",  ra:21.4994, dec:12.167  },
  {id:"M16", name:"Eagle Nebula",          type:"Emission Nebula",   ra:18.3128, dec:-13.791 },
  {id:"M17", name:"Omega Nebula",          type:"Emission Nebula",   ra:18.3461, dec:-16.177 },
  {id:"M18", name:"",                      type:"Open Cluster",      ra:18.3319, dec:-17.100 },
  {id:"M19", name:"",                      type:"Globular Cluster",  ra:17.0444, dec:-26.269 },
  {id:"M20", name:"Trifid Nebula",         type:"Emission Nebula",   ra:18.0428, dec:-23.032 },
  {id:"M21", name:"",                      type:"Open Cluster",      ra:18.0772, dec:-22.493 },
  {id:"M22", name:"Sagittarius Cluster",   type:"Globular Cluster",  ra:18.6072, dec:-23.905 },
  {id:"M23", name:"",                      type:"Open Cluster",      ra:17.9478, dec:-18.986 },
  {id:"M24", name:"Sagittarius Star Cloud",type:"Star Cloud",        ra:18.2836, dec:-18.553 },
  {id:"M25", name:"",                      type:"Open Cluster",      ra:18.5272, dec:-19.250 },
  {id:"M26", name:"",                      type:"Open Cluster",      ra:18.7544, dec:-9.384  },
  {id:"M27", name:"Dumbbell Nebula",       type:"Planetary Nebula",  ra:19.9936, dec:22.721  },
  {id:"M28", name:"",                      type:"Globular Cluster",  ra:18.4094, dec:-24.870 },
  {id:"M29", name:"",                      type:"Open Cluster",      ra:20.3986, dec:38.524  },
  {id:"M30", name:"",                      type:"Globular Cluster",  ra:21.6722, dec:-23.180 },
  {id:"M31", name:"Andromeda Galaxy",      type:"Galaxy",            ra:0.7122,  dec:41.269  },
  {id:"M32", name:"",                      type:"Galaxy",            ra:0.7119,  dec:40.866  },
  {id:"M33", name:"Triangulum Galaxy",     type:"Galaxy",            ra:1.5644,  dec:30.660  },
  {id:"M34", name:"",                      type:"Open Cluster",      ra:2.7019,  dec:42.748  },
  {id:"M35", name:"",                      type:"Open Cluster",      ra:6.1486,  dec:24.333  },
  {id:"M36", name:"",                      type:"Open Cluster",      ra:5.6028,  dec:34.134  },
  {id:"M37", name:"",                      type:"Open Cluster",      ra:5.8719,  dec:32.551  },
  {id:"M38", name:"",                      type:"Open Cluster",      ra:5.4786,  dec:35.852  },
  {id:"M39", name:"",                      type:"Open Cluster",      ra:21.5297, dec:48.427  },
  {id:"M40", name:"Winnecke 4",            type:"Double Star",       ra:12.3672, dec:58.085  },
  {id:"M41", name:"",                      type:"Open Cluster",      ra:6.7769,  dec:-20.721 },
  {id:"M42", name:"Orion Nebula",          type:"Emission Nebula",   ra:5.5881,  dec:-5.391  },
  {id:"M43", name:"De Mairan's Nebula",    type:"Emission Nebula",   ra:5.5931,  dec:-5.271  },
  {id:"M44", name:"Beehive Cluster",       type:"Open Cluster",      ra:8.6719,  dec:19.994  },
  {id:"M45", name:"Pleiades",              type:"Open Cluster",      ra:3.7908,  dec:24.117  },
  {id:"M46", name:"",                      type:"Open Cluster",      ra:7.6961,  dec:-14.816 },
  {id:"M47", name:"",                      type:"Open Cluster",      ra:7.6094,  dec:-14.490 },
  {id:"M48", name:"",                      type:"Open Cluster",      ra:8.2297,  dec:-5.717  },
  {id:"M49", name:"",                      type:"Galaxy",            ra:12.4961, dec:8.000   },
  {id:"M50", name:"",                      type:"Open Cluster",      ra:7.0461,  dec:-8.366  },
  {id:"M51", name:"Whirlpool Galaxy",      type:"Galaxy",            ra:13.4978, dec:47.195  },
  {id:"M52", name:"",                      type:"Open Cluster",      ra:23.4019, dec:61.593  },
  {id:"M53", name:"",                      type:"Globular Cluster",  ra:13.2156, dec:18.169  },
  {id:"M54", name:"",                      type:"Globular Cluster",  ra:18.9178, dec:-30.478 },
  {id:"M55", name:"Summer Rose Star",      type:"Globular Cluster",  ra:19.6667, dec:-30.964 },
  {id:"M56", name:"",                      type:"Globular Cluster",  ra:19.2767, dec:30.185  },
  {id:"M57", name:"Ring Nebula",           type:"Planetary Nebula",  ra:18.8933, dec:33.028  },
  {id:"M58", name:"",                      type:"Galaxy",            ra:12.6278, dec:11.818  },
  {id:"M59", name:"",                      type:"Galaxy",            ra:12.7003, dec:11.647  },
  {id:"M60", name:"",                      type:"Galaxy",            ra:12.7272, dec:11.553  },
  {id:"M61", name:"",                      type:"Galaxy",            ra:12.3656, dec:4.474   },
  {id:"M62", name:"",                      type:"Globular Cluster",  ra:17.0194, dec:-30.112 },
  {id:"M63", name:"Sunflower Galaxy",      type:"Galaxy",            ra:13.2636, dec:42.029  },
  {id:"M64", name:"Black Eye Galaxy",      type:"Galaxy",            ra:12.9461, dec:21.683  },
  {id:"M65", name:"",                      type:"Galaxy",            ra:11.3172, dec:13.092  },
  {id:"M66", name:"",                      type:"Galaxy",            ra:11.3367, dec:12.991  },
  {id:"M67", name:"",                      type:"Open Cluster",      ra:8.8544,  dec:11.816  },
  {id:"M68", name:"",                      type:"Globular Cluster",  ra:12.6572, dec:-26.746 },
  {id:"M69", name:"",                      type:"Globular Cluster",  ra:18.5231, dec:-32.348 },
  {id:"M70", name:"",                      type:"Globular Cluster",  ra:18.7217, dec:-32.294 },
  {id:"M71", name:"",                      type:"Globular Cluster",  ra:19.8961, dec:18.779  },
  {id:"M72", name:"",                      type:"Globular Cluster",  ra:20.8911, dec:-12.537 },
  {id:"M73", name:"",                      type:"Asterism",          ra:20.9839, dec:-12.633 },
  {id:"M74", name:"",                      type:"Galaxy",            ra:1.6111,  dec:15.783  },
  {id:"M75", name:"",                      type:"Globular Cluster",  ra:20.1014, dec:-21.921 },
  {id:"M76", name:"Little Dumbbell Nebula",type:"Planetary Nebula",  ra:1.7031,  dec:51.575  },
  {id:"M77", name:"",                      type:"Galaxy",            ra:2.7119,  dec:-0.013  },
  {id:"M78", name:"",                      type:"Reflection Nebula", ra:5.7786,  dec:0.078   },
  {id:"M79", name:"",                      type:"Globular Cluster",  ra:5.4047,  dec:-24.523 },
  {id:"M80", name:"",                      type:"Globular Cluster",  ra:16.2856, dec:-22.976 },
  {id:"M81", name:"Bode's Galaxy",         type:"Galaxy",            ra:9.9258,  dec:69.065  },
  {id:"M82", name:"Cigar Galaxy",          type:"Galaxy",            ra:9.9256,  dec:69.680  },
  {id:"M83", name:"Southern Pinwheel",     type:"Galaxy",            ra:13.6169, dec:-29.866 },
  {id:"M84", name:"",                      type:"Galaxy",            ra:12.4183, dec:12.887  },
  {id:"M85", name:"",                      type:"Galaxy",            ra:12.4228, dec:18.191  },
  {id:"M86", name:"",                      type:"Galaxy",            ra:12.4353, dec:12.946  },
  {id:"M87", name:"Virgo A",               type:"Galaxy",            ra:12.5136, dec:12.391  },
  {id:"M88", name:"",                      type:"Galaxy",            ra:12.5319, dec:14.420  },
  {id:"M89", name:"",                      type:"Galaxy",            ra:12.5944, dec:12.556  },
  {id:"M90", name:"",                      type:"Galaxy",            ra:12.6136, dec:13.163  },
  {id:"M91", name:"",                      type:"Galaxy",            ra:12.5931, dec:14.496  },
  {id:"M92", name:"",                      type:"Globular Cluster",  ra:17.2856, dec:43.136  },
  {id:"M93", name:"",                      type:"Open Cluster",      ra:7.7428,  dec:-23.856 },
  {id:"M94", name:"",                      type:"Galaxy",            ra:12.8478, dec:41.120  },
  {id:"M95", name:"",                      type:"Galaxy",            ra:10.7331, dec:11.704  },
  {id:"M96", name:"",                      type:"Galaxy",            ra:10.7794, dec:11.820  },
  {id:"M97", name:"Owl Nebula",            type:"Planetary Nebula",  ra:11.2478, dec:55.019  },
  {id:"M98", name:"",                      type:"Galaxy",            ra:12.2314, dec:14.901  },
  {id:"M99", name:"",                      type:"Galaxy",            ra:12.3136, dec:14.416  },
  {id:"M100",name:"",                      type:"Galaxy",            ra:12.3817, dec:15.823  },
  {id:"M101",name:"Pinwheel Galaxy",       type:"Galaxy",            ra:14.0536, dec:54.349  },
  {id:"M102",name:"Spindle Galaxy",        type:"Galaxy",            ra:15.1028, dec:55.763  },
  {id:"M103",name:"",                      type:"Open Cluster",      ra:1.5572,  dec:60.657  },
  {id:"M104",name:"Sombrero Galaxy",       type:"Galaxy",            ra:12.6664, dec:-11.623 },
  {id:"M105",name:"",                      type:"Galaxy",            ra:10.7972, dec:12.581  },
  {id:"M106",name:"",                      type:"Galaxy",            ra:12.3161, dec:47.304  },
  {id:"M107",name:"",                      type:"Globular Cluster",  ra:16.5417, dec:-13.054 },
  {id:"M108",name:"",                      type:"Galaxy",            ra:11.1919, dec:55.674  },
  {id:"M109",name:"",                      type:"Galaxy",            ra:11.9583, dec:53.374  },
  {id:"M110",name:"",                      type:"Galaxy",            ra:0.6728,  dec:41.685  },
];

let _catalogIdx = -1;

function catalogFilter() {
  const q   = document.getElementById("catalogSearch").value.trim().toLowerCase();
  const dd  = document.getElementById("catalogDropdown");
  _catalogIdx = -1;

  const matches = q.length === 0 ? [] : MESSIER_CATALOG.filter(o => {
    const label = (o.id + " " + o.name + " " + o.type).toLowerCase();
    return q.split(/\s+/).every(t => label.includes(t));
  }).slice(0, 30);

  if (matches.length === 0) { dd.style.display = "none"; return; }

  dd.innerHTML = matches.map((o, i) => {
    const label = o.name ? `${o.id} — ${o.name}` : o.id;
    return `<div class="cat-item" data-idx="${i}"
      onmousedown="catalogSelect(${i})" onmouseover="catalogHover(${i})"
      style="padding:7px 10px;cursor:pointer;font-size:12px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">
      <span style="color:var(--text);">${label}</span>
      <span style="font-size:10px;color:var(--dim);letter-spacing:1px;">${o.type.toUpperCase()}</span>
    </div>`;
  }).join("");
  dd._matches = matches;
  dd.style.display = "block";
}

function catalogHover(i) {
  _catalogIdx = i;
  document.querySelectorAll(".cat-item").forEach((el, j) => {
    el.style.background = j === i ? "var(--border)" : "";
  });
}

function catalogSelect(i) {
  const dd  = document.getElementById("catalogDropdown");
  const obj = dd._matches[i];
  if (!obj) return;
  setSlewMode("eq");
  document.getElementById("slewRA").value  = obj.ra.toFixed(4);
  document.getElementById("slewDec").value = obj.dec.toFixed(4);
  document.getElementById("catalogSearch").value = obj.name ? `${obj.id} — ${obj.name}` : obj.id;
  dd.style.display = "none";
}

function catalogKeyNav(e) {
  const dd = document.getElementById("catalogDropdown");
  if (dd.style.display === "none") return;
  const items = dd._matches || [];
  if (e.key === "ArrowDown") {
    e.preventDefault();
    _catalogIdx = Math.min(_catalogIdx + 1, items.length - 1);
    catalogHover(_catalogIdx);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    _catalogIdx = Math.max(_catalogIdx - 1, 0);
    catalogHover(_catalogIdx);
  } else if (e.key === "Enter" && _catalogIdx >= 0) {
    e.preventDefault();
    catalogSelect(_catalogIdx);
  } else if (e.key === "Escape") {
    dd.style.display = "none";
  }
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#catalogSearch") && !e.target.closest("#catalogDropdown")) {
    const dd = document.getElementById("catalogDropdown");
    if (dd) dd.style.display = "none";
  }
});

let _slewMode = "eq";

function setSlewMode(mode) {
  _slewMode = mode;
  const isAltAz = mode === "altaz";
  document.getElementById("slewInputsEQ").style.display    = isAltAz ? "none" : "";
  document.getElementById("slewInputsAltAz").style.display = isAltAz ? "" : "none";
  document.getElementById("slewModeEQ").style.background    = isAltAz ? "var(--panel-bg)" : "var(--blue)";
  document.getElementById("slewModeEQ").style.color         = isAltAz ? "var(--dim)" : "#fff";
  document.getElementById("slewModeAltAz").style.background = isAltAz ? "var(--blue)" : "var(--panel-bg)";
  document.getElementById("slewModeAltAz").style.color      = isAltAz ? "#fff" : "var(--dim)";
}

async function apiSlew() {
  const btn = document.getElementById("btnModalSlew");
  let payload;
  if (_slewMode === "altaz") {
    const alt = parseFloat(document.getElementById("slewAlt").value);
    const az  = parseFloat(document.getElementById("slewAz").value);
    if (isNaN(alt) || isNaN(az)) { alert("Enter valid Altitude (°) and Azimuth (°) values."); return; }
    payload = { mode: "altaz", alt, az };
  } else {
    const ra  = parseFloat(document.getElementById("slewRA").value);
    const dec = parseFloat(document.getElementById("slewDec").value);
    if (isNaN(ra) || isNaN(dec)) { alert("Enter valid RA (h) and Dec (°) values."); return; }
    payload = { mode: "eq", ra, dec };
  }
  btn.disabled = true; btn.textContent = "Slewing…";
  try {
    const r = await fetch("/api/slew", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (r.status === 403 && d.horizon_blocked) {
      alert(
        "⛔ Horizon mask blocked this slew\n\n" +
        d.error +
        (d.min_safe_alt != null
          ? "\n\nMinimum safe altitude in that direction: " + d.min_safe_alt + "°"
          : "")
      );
    } else if (!d.ok) {
      alert(d.error || "Slew failed");
    }
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

// ── Sky Mask (12-spoke radial drag) ──────────────────────────────────────────

(function () {
  const W = 360, H = 360, CX = 180, CY = 180, MAX_R = 155;
  const N = 12, AZ_STEP = 30;
  const DIR_LABELS = ["N","NNE","ENE","E","ESE","SSE","S","SSW","WSW","W","WNW","NNW"];

  let alts       = new Array(N).fill(0);
  let ctx        = null;
  let dragIdx    = -1;
  let hovIdx     = -1;
  let _scanOvly  = null;   // set by window.setScanOverlay from the scan polling loop

  function toXY(alt, az) {
    const r = MAX_R * (1 - alt / 90);
    const a = az * Math.PI / 180;
    return [CX + r * Math.sin(a), CY - r * Math.cos(a)];
  }

  function handleXY(i)  { return toXY(alts[i], i * AZ_STEP); }

  function altFromMouse(i, cx, cy) {
    const a  = (i * AZ_STEP) * Math.PI / 180;
    const mx = cx - CX, my = cy - CY;
    // Project mouse onto the radial direction for this spoke
    const r  = mx * Math.sin(a) + my * (-Math.cos(a));
    return Math.round(90 * (1 - Math.max(0, Math.min(MAX_R, r)) / MAX_R) * 10) / 10;
  }

  function findHandle(cx, cy) {
    let best = -1, bestD = 14;
    for (let i = 0; i < N; i++) {
      const [hx, hy] = handleXY(i);
      const d = Math.sqrt((cx - hx) ** 2 + (cy - hy) ** 2);
      if (d < bestD) { best = i; bestD = d; }
    }
    return best;
  }

  function draw() {
    if (!ctx) return;
    ctx.clearRect(0, 0, W, H);

    // Background disk
    ctx.beginPath();
    ctx.arc(CX, CY, MAX_R, 0, 2 * Math.PI);
    ctx.fillStyle = "#080c12";
    ctx.fill();

    // Altitude rings at 30° and 60°
    ctx.save();
    ctx.setLineDash([3, 6]);
    ctx.strokeStyle = "#1e2936";
    ctx.lineWidth = 1;
    for (const alt of [30, 60]) {
      const r = MAX_R * (1 - alt / 90);
      ctx.beginPath();
      ctx.arc(CX, CY, r, 0, 2 * Math.PI);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.restore();

    // Azimuth spokes — draw all 12 (one per handle)
    ctx.save();
    ctx.lineWidth = 1;
    for (let i = 0; i < N; i++) {
      const az = i * AZ_STEP;
      const [x, y] = toXY(0, az);
      ctx.strokeStyle = (i === hovIdx || i === dragIdx) ? "#2a4060" : "#1e2936";
      ctx.beginPath();
      ctx.moveTo(CX, CY);
      ctx.lineTo(x, y);
      ctx.stroke();
    }
    ctx.restore();

    // Horizon ring
    ctx.beginPath();
    ctx.arc(CX, CY, MAX_R, 0, 2 * Math.PI);
    ctx.strokeStyle = "#2d3d4d";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Altitude ring labels
    ctx.save();
    ctx.font = "9px monospace";
    ctx.fillStyle = "#3a4a56";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    for (const alt of [30, 60]) {
      const r = MAX_R * (1 - alt / 90);
      ctx.fillText(alt + "°", CX + 4, CY - r + 2);
    }
    ctx.restore();

    // Cardinal labels
    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const dirs = [
      [0,   "N",  "#56d364", true ],
      [90,  "E",  "#3a4a56", false],
      [180, "S",  "#3a4a56", false],
      [270, "W",  "#3a4a56", false],
    ];
    for (const [az, label, color, bold] of dirs) {
      const a  = az * Math.PI / 180;
      const lx = CX + (MAX_R + 14) * Math.sin(a);
      const ly = CY - (MAX_R + 14) * Math.cos(a);
      ctx.font = (bold ? "bold " : "") + "11px monospace";
      ctx.fillStyle = color;
      ctx.fillText(label, lx, ly);
    }
    ctx.restore();

    // Zenith dot
    ctx.beginPath();
    ctx.arc(CX, CY, 3, 0, 2 * Math.PI);
    ctx.fillStyle = "#2d3d4d";
    ctx.fill();

    // Safe-zone polygon (always closed, all 12 handles)
    const coords = alts.map((_, i) => handleXY(i));
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(coords[0][0], coords[0][1]);
    for (let i = 1; i < N; i++) ctx.lineTo(coords[i][0], coords[i][1]);
    ctx.closePath();
    ctx.fillStyle = "rgba(63,185,80,0.13)";
    ctx.fill();
    ctx.strokeStyle = "#3fb950";
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();
    ctx.restore();

    // ── Scan overlay ──────────────────────────────────────────────────────────
    if (_scanOvly && _scanOvly.directions && _scanOvly.directions.length === N) {
      const dirs = _scanOvly.directions;

      // Colour-code each spoke by status
      for (let i = 0; i < N; i++) {
        const d   = dirs[i];
        const az  = i * AZ_STEP;
        const a   = az * Math.PI / 180;
        const [ex, ey] = toXY(0, az);
        const col = d.status === 'scanning' ? '#f0c040'
                  : d.status === 'done'     ? (d.horizon_alt === 0 ? '#3fb950' : '#e06030')
                  : '#243040';  // pending
        ctx.save();
        ctx.strokeStyle = col;
        ctx.lineWidth   = d.status === 'scanning' ? 2.5 : 1.5;
        ctx.globalAlpha = d.status === 'pending'  ? 0.4  : 0.85;
        ctx.beginPath(); ctx.moveTo(CX, CY); ctx.lineTo(ex, ey); ctx.stroke();
        ctx.restore();

        // Dots at each tested altitude along this spoke
        if (d.steps && d.steps.length) {
          for (const step of d.steps) {
            if (step.alt == null) continue;
            const [sx, sy] = toXY(step.alt, az);
            const hasSky   = step.stars != null && step.stars >= 0;
            const clear    = step.stars > 0;
            ctx.save();
            ctx.beginPath();
            ctx.arc(sx, sy, 3, 0, 2 * Math.PI);
            ctx.fillStyle   = clear ? '#56d364' : (hasSky ? '#e06030' : '#888');
            ctx.globalAlpha = 0.9;
            ctx.fill();
            ctx.restore();
          }
        }

        // Rim label: show found altitude for done directions
        if (d.status === 'done' || d.status === 'scanning') {
          const lx = CX + (MAX_R + 14) * Math.sin(a);
          const ly = CY - (MAX_R + 14) * Math.cos(a);
          ctx.save();
          ctx.font          = "bold 10px monospace";
          ctx.fillStyle     = d.status === 'scanning' ? '#f0c040' : (d.horizon_alt === 0 ? '#3fb950' : '#e06030');
          ctx.textAlign     = "center";
          ctx.textBaseline  = "middle";
          const rimLbl = d.status === 'scanning' ? '…'
                       : (d.horizon_alt === 0 ? DIR_LABELS[i] + ' ✓' : DIR_LABELS[i] + ' ' + d.horizon_alt + '°');
          ctx.fillText(rimLbl, lx, ly);
          ctx.restore();
        }
      }

      // If scan result exists, draw it as a faint orange preview polygon
      if (_scanOvly.result && _scanOvly.result.length === N) {
        const rcoords = _scanOvly.result.map(p => toXY(p[0], p[1]));
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(rcoords[0][0], rcoords[0][1]);
        for (let i = 1; i < N; i++) ctx.lineTo(rcoords[i][0], rcoords[i][1]);
        ctx.closePath();
        ctx.fillStyle   = "rgba(224,96,48,0.15)";
        ctx.fill();
        ctx.strokeStyle = "#e06030";
        ctx.lineWidth   = 1.5;
        ctx.setLineDash([5, 3]);
        ctx.stroke();
        ctx.restore();
      }
    }
    // ── /Scan overlay ─────────────────────────────────────────────────────────

    // Handles
    for (let i = 0; i < N; i++) {
      const [hx, hy] = coords[i];
      const isDragging = (i === dragIdx);
      const isHovered  = (i === hovIdx);
      const active = isDragging || isHovered;
      ctx.save();
      if (active) {
        ctx.shadowColor = isDragging ? "#f0c040" : "#58a6ff";
        ctx.shadowBlur  = isDragging ? 18 : 10;
      }
      ctx.beginPath();
      ctx.arc(hx, hy, isDragging ? 9 : (isHovered ? 7 : 5), 0, 2 * Math.PI);
      ctx.fillStyle = isDragging ? "#f0c040" : (isHovered ? "#58a6ff" : "#3fb950");
      ctx.fill();
      ctx.restore();

      // Spoke label at rim for hovered handle
      if (active && !isDragging) {
        ctx.save();
        ctx.font = "bold 10px monospace";
        ctx.fillStyle = "#58a6ff";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const a  = (i * AZ_STEP) * Math.PI / 180;
        const lx = CX + (MAX_R + 14) * Math.sin(a);
        const ly = CY - (MAX_R + 14) * Math.cos(a);
        ctx.fillText(DIR_LABELS[i], lx, ly);
        ctx.restore();
      }
    }

    // ── Big on-canvas readout while dragging ─────────────────────────────────
    if (dragIdx >= 0) {
      const i   = dragIdx;
      const alt = alts[i];
      const [hx, hy] = coords[i];
      const label = DIR_LABELS[i] + "  " + alt.toFixed(1) + "°";

      // Spoke label at rim
      ctx.save();
      ctx.font = "bold 11px monospace";
      ctx.fillStyle = "#f0c040";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const spA = (i * AZ_STEP) * Math.PI / 180;
      ctx.fillText(DIR_LABELS[i], CX + (MAX_R + 14) * Math.sin(spA), CY - (MAX_R + 14) * Math.cos(spA));
      ctx.restore();

      // Radial dotted guide line from center to handle
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(240,192,64,0.45)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(CX, CY);
      ctx.lineTo(hx, hy);
      ctx.stroke();
      ctx.restore();

      // Floating pill near the handle showing altitude
      ctx.save();
      ctx.font = "bold 18px monospace";
      const tw = ctx.measureText(label).width;
      const pw = tw + 18, ph = 28;
      // Position pill: offset from handle, keep inside canvas
      let px = hx + 14, py = hy - ph / 2;
      if (px + pw > W - 4) px = hx - pw - 14;
      if (py < 4)          py = 4;
      if (py + ph > H - 4) py = H - ph - 4;

      // Pill background
      ctx.beginPath();
      ctx.roundRect(px, py, pw, ph, 6);
      ctx.fillStyle = "rgba(15,22,30,0.92)";
      ctx.fill();
      ctx.strokeStyle = "#f0c040";
      ctx.lineWidth = 1.5;
      ctx.stroke();

      // Pill text
      ctx.fillStyle = "#f0c040";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(label, px + 9, py + ph / 2);
      ctx.restore();
    }

    // DOM info strip update
    const activeIdx = dragIdx >= 0 ? dragIdx : hovIdx;
    if (activeIdx >= 0) {
      const infoEl = document.getElementById("skyCoordInfo");
      if (infoEl) {
        infoEl.textContent =
          DIR_LABELS[activeIdx] + "  Az " + (activeIdx * AZ_STEP) + "°  Alt " +
          alts[activeIdx].toFixed(1) + "°";
      }
    }
  }

  function updateInfo() {
    const infoEl = document.getElementById("skyCoordInfo");
    const hintEl = document.getElementById("skyHint");
    const activeIdx = dragIdx >= 0 ? dragIdx : hovIdx;
    if (infoEl && activeIdx < 0) infoEl.textContent = "";
    if (hintEl) {
      hintEl.textContent = dragIdx >= 0
        ? "Drag radially to change altitude"
        : "Drag a handle to set min altitude";
    }
  }

  function canvasPos(canvas, e) {
    const rect = canvas.getBoundingClientRect();
    return [
      (e.clientX - rect.left) * (canvas.width  / rect.width),
      (e.clientY - rect.top)  * (canvas.height / rect.height),
    ];
  }

  window.loadSkyMask = async function () {
    const canvas = document.getElementById("skyCanvas");
    if (canvas && !ctx) ctx = canvas.getContext("2d");
    alts = new Array(N).fill(0);
    try {
      const r = await fetch("/api/safety/horizon-mask");
      const d = await r.json();
      (d.polygon || []).forEach(function (p) {
        const az  = ((p[1] % 360) + 360) % 360;
        const idx = Math.round(az / AZ_STEP) % N;
        alts[idx] = Math.max(0, Math.min(90, p[0]));
      });
    } catch (_) {}
    dragIdx = -1; hovIdx = -1;
    const btn = document.getElementById("btnSkyMaskSave");
    if (btn) { btn.textContent = "Save Mask"; btn.disabled = false; }
    draw();
    updateInfo();
  };

  window.clearSkyMask = function () {
    alts = new Array(N).fill(0);
    dragIdx = -1; hovIdx = -1;
    draw(); updateInfo();
  };

  window.setScanOverlay = function (state) {
    _scanOvly = state;
    draw();
  };

  window.loadAltsFromResult = function (result) {
    // result: [[alt, az], …]
    alts = new Array(N).fill(0);
    (result || []).forEach(function (p) {
      const az  = ((p[1] % 360) + 360) % 360;
      const idx = Math.round(az / AZ_STEP) % N;
      alts[idx] = Math.max(0, Math.min(90, p[0]));
    });
    _scanOvly = null;
    draw(); updateInfo();
  };

  window.saveSkyMask = async function () {
    const btn = document.getElementById("btnSkyMaskSave");
    btn.disabled = true; btn.textContent = "Saving…";
    try {
      const polygon = alts.map(function (alt, i) { return [alt, i * AZ_STEP]; });
      const r = await fetch("/api/safety/horizon-mask", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ polygon }),
      });
      const d = await r.json();
      if (d.ok) {
        btn.textContent = "Saved ✓";
        setTimeout(function () { btn.textContent = "Save Mask"; btn.disabled = false; }, 1800);
      } else {
        alert("Save failed: " + (d.error || "unknown"));
        btn.textContent = "Save Mask"; btn.disabled = false;
      }
    } catch (e) {
      alert("Save failed: " + e.message);
      btn.textContent = "Save Mask"; btn.disabled = false;
    }
  };

  document.addEventListener("DOMContentLoaded", function () {
    const canvas = document.getElementById("skyCanvas");
    if (!canvas) return;
    ctx = canvas.getContext("2d");
    draw();

    canvas.addEventListener("pointermove", function (e) {
      const [cx, cy] = canvasPos(canvas, e);
      if (dragIdx >= 0) {
        alts[dragIdx] = altFromMouse(dragIdx, cx, cy);
        draw(); updateInfo();
      } else {
        const h = findHandle(cx, cy);
        if (h !== hovIdx) { hovIdx = h; draw(); updateInfo(); }
      }
    });

    canvas.addEventListener("pointerdown", function (e) {
      const [cx, cy] = canvasPos(canvas, e);
      const h = findHandle(cx, cy);
      if (h < 0) return;
      dragIdx = h; hovIdx = -1;
      canvas.setPointerCapture(e.pointerId);
      canvas.style.cursor = "ns-resize";
      draw(); updateInfo();
    });

    canvas.addEventListener("pointerup", function (e) {
      dragIdx = -1;
      canvas.style.cursor = "default";
      const [cx, cy] = canvasPos(canvas, e);
      hovIdx = findHandle(cx, cy);
      draw(); updateInfo();
    });

    canvas.addEventListener("pointercancel", function () {
      dragIdx = -1; hovIdx = -1;
      canvas.style.cursor = "default";
      draw(); updateInfo();
    });

    canvas.addEventListener("pointerleave", function () {
      if (dragIdx < 0) { hovIdx = -1; draw(); updateInfo(); }
    });
  });
})();

// ── Horizon scan controls ────────────────────────────────────────────────────

(function () {
  const DIR_LABELS = ["N","NNE","ENE","E","ESE","SSE","S","SSW","WSW","W","WNW","NNW"];
  let _pollTimer  = null;
  let _scanResult = null;

  function _scanEl(id) { return document.getElementById(id); }

  function _setScanUI(running, hasResult) {
    const start  = _scanEl("btnStartScan");
    const cancel = _scanEl("btnCancelScan");
    const apply  = _scanEl("btnApplyScan");
    const fields = _scanEl("scanConfigFields");
    const prog   = _scanEl("scanProgress");
    if (start)  start.style.display  = running || hasResult ? "none" : "";
    if (cancel) cancel.style.display = running ? "" : "none";
    if (apply)  apply.style.display  = (!running && hasResult) ? "" : "none";
    if (fields) fields.style.opacity = running ? "0.4" : "1";
    if (prog)   prog.style.display   = (running || hasResult) ? "" : "none";
  }

  function _updateProgressUI(state) {
    const label = _scanEl("scanProgressLabel");
    const bars  = _scanEl("scanDirectionBars");
    if (!label || !bars || !state.directions) return;

    // Status line
    const scanning = state.directions.find(d => d.status === "scanning");
    const done     = state.directions.filter(d => d.status === "done").length;
    if (state.running && scanning) {
      label.textContent = `Scanning ${DIR_LABELS[state.directions.indexOf(scanning)]} (${done}/12)…`;
      label.style.color = "var(--blue)";
    } else if (!state.running && state.error) {
      label.textContent = "Scan failed: " + state.error;
      label.style.color = "var(--red)";
    } else if (!state.running && state.result) {
      label.textContent = "Scan complete — review the dashed orange profile, then Apply.";
      label.style.color = "var(--green)";
    }

    // Per-direction bar chart
    bars.innerHTML = "";
    state.directions.forEach(function (d, i) {
      const bar = document.createElement("div");
      bar.title = DIR_LABELS[i] + (d.horizon_alt != null ? "  " + d.horizon_alt + "°" : "");
      const heightPct = d.status === "done"
        ? Math.max(4, Math.round((d.horizon_alt / 90) * 100))
        : (d.status === "scanning" ? 50 : 4);
      bar.style.cssText = [
        "flex:1", "border-radius:2px 2px 0 0", "transition:height .3s",
        "height:" + heightPct + "%",
        "background:" + (d.status === "done"
          ? (d.horizon_alt === 0 ? "var(--green)" : "#e06030")
          : (d.status === "scanning" ? "#f0c040" : "var(--border)")),
      ].join(";");
      bars.appendChild(bar);
    });
  }

  async function _poll() {
    try {
      const r = await fetch("/api/safety/horizon-scan/status");
      const s = await r.json();
      window.setScanOverlay(s);
      _updateProgressUI(s);
      _setScanUI(s.running, !!s.result);
      if (!s.running) {
        clearInterval(_pollTimer); _pollTimer = null;
        _scanResult = s.result || null;
      }
    } catch (_) {}
  }

  window.startHorizonScan = async function () {
    const params = {
      floor_alt:      parseFloat(_scanEl("scanFloorAlt")?.value)    || 25,
      start_alt:      parseFloat(_scanEl("scanStartAlt")?.value)    || 60,
      step:           parseFloat(_scanEl("scanStep")?.value)        || 5,
      exposure:       parseFloat(_scanEl("scanExposure")?.value)    || 5,
      star_threshold: parseInt(  _scanEl("scanStarThresh")?.value)  || 5,
      settle:         parseFloat(_scanEl("scanSettle")?.value)      || 2,
    };
    _scanResult = null;
    _setScanUI(true, false);
    try {
      const r = await fetch("/api/safety/horizon-scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
      });
      const d = await r.json();
      if (!d.ok) {
        alert("Scan failed to start: " + (d.error || "unknown"));
        _setScanUI(false, false); return;
      }
    } catch (e) {
      alert("Scan failed to start: " + e.message);
      _setScanUI(false, false); return;
    }
    _pollTimer = setInterval(_poll, 1500);
    _poll();
  };

  window.cancelHorizonScan = async function () {
    try {
      await fetch("/api/safety/horizon-scan", { method: "DELETE" });
    } catch (_) {}
    clearInterval(_pollTimer); _pollTimer = null;
    window.setScanOverlay(null);
    _setScanUI(false, false);
    const label = _scanEl("scanProgressLabel");
    if (label) { label.textContent = "Scan cancelled."; label.style.color = "var(--dim)"; }
  };

  window.applyHorizonScanResult = function () {
    if (!_scanResult) return;
    window.loadAltsFromResult(_scanResult);
    _scanResult = null;
    _setScanUI(false, false);
    const prog = _scanEl("scanProgress");
    if (prog) prog.style.display = "none";
    // Prompt the user to review and save
    const btn = document.getElementById("btnSkyMaskSave");
    if (btn) {
      btn.textContent = "Save Mask ← review & save!";
      btn.style.animation = "pulse 1s ease 3";
      setTimeout(function () {
        btn.textContent = "Save Mask";
        btn.style.animation = "";
      }, 4000);
    }
  };

  // Re-poll on tab open in case a scan is already running
  document.addEventListener("DOMContentLoaded", function () {
    fetch("/api/safety/horizon-scan/status").then(r => r.json()).then(function (s) {
      if (s.running || s.result) {
        window.setScanOverlay(s);
        _updateProgressUI(s);
        _setScanUI(s.running, !!s.result);
        _scanResult = s.result || null;
        if (s.running) _pollTimer = setInterval(_poll, 1500);
      }
    }).catch(function () {});
  });
})();

// ── Discovery overlay ────────────────────────────────────────────────────────

function showDiscover() {
  document.getElementById("overlay").classList.remove("hidden");
  document.getElementById("btnDiscover").textContent = "● Scanning";
  doScan();
}
function hideDiscover() {
  document.getElementById("overlay").classList.add("hidden");
  document.getElementById("btnDiscover").textContent = "Discover";
}

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
  const discoverBtn = document.getElementById("btnDiscover");
  if (discoverBtn && document.getElementById("overlay").classList.contains("hidden")) {
    discoverBtn.textContent = "Discover";
  }
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

// ── Config editor ─────────────────────────────────────────────────────────────

let _cfgView      = 'form';
let _cfgParsed    = {};
let _cfgActiveTab = 'setup';
const _CFG_TABS   = ['setup','photometry','aavso','safety','advanced'];

function switchCfgTab(tab) {
  _cfgActiveTab = tab;
  _CFG_TABS.forEach(t => {
    const panel = document.getElementById('cfgPanel_' + t);
    const btn   = document.getElementById('cfgTab_'   + t);
    if (panel) panel.style.display = (t === tab) ? '' : 'none';
    if (btn)   btn.classList.toggle('active', t === tab);
  });
  if (tab === 'horizon') window.loadSkyMask();
}

function _cfgGet(obj, path, fallback) {
  const parts = path.split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return (fallback !== undefined) ? fallback : '';
    cur = cur[p];
  }
  return (cur != null) ? cur : ((fallback !== undefined) ? fallback : '');
}

function renderCfgForm(c) {
  function setVal(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = Boolean(val);
    else el.value = (val != null) ? val : '';
  }
  setVal('cfgAlpacaPort',    _cfgGet(c, 'alpaca.discovery_port',    32227));
  setVal('cfgAlpacaTimeout', _cfgGet(c, 'alpaca.discovery_timeout', 5));
  setVal('cfgAlpacaApiVer',  _cfgGet(c, 'alpaca.api_version',       1));
  setVal('cfgObsLat',        _cfgGet(c, 'safety.observer.latitude',  0.0));
  setVal('cfgObsLon',        _cfgGet(c, 'safety.observer.longitude', 0.0));
  setVal('cfgDevTelEnabled', _cfgGet(c, 'devices.telescope.enabled',         false));
  setVal('cfgDevTelNum',     _cfgGet(c, 'devices.telescope.device_number',   0));
  setVal('cfgDevCamEnabled', _cfgGet(c, 'devices.camera.enabled',            false));
  setVal('cfgDevCamNum',     _cfgGet(c, 'devices.camera.device_number',      0));
  setVal('cfgDevFocEnabled', _cfgGet(c, 'devices.focuser.enabled',           false));
  setVal('cfgDevFocNum',     _cfgGet(c, 'devices.focuser.device_number',     0));
  setVal('cfgDevFwEnabled',  _cfgGet(c, 'devices.filterwheel.enabled',       false));
  setVal('cfgDevFwNum',      _cfgGet(c, 'devices.filterwheel.device_number', 0));
  setVal('cfgTrackingRate',  _cfgGet(c, 'telescope.tracking_rate', 0));
  setVal('cfgCamExposure',   _cfgGet(c, 'camera.exposure_duration', 1.0));
  setVal('cfgCamBinning',    _cfgGet(c, 'camera.binning',           1));
  setVal('cfgSafetyEnabled',       _cfgGet(c, 'safety.enabled',              true));
  setVal('cfgSafetyParkDawn',      _cfgGet(c, 'safety.park_at_dawn',         true));
  setVal('cfgSafetyDawnType',      _cfgGet(c, 'safety.dawn_type',    'astronomical'));
  setVal('cfgSafetyHb',            _cfgGet(c, 'safety.heartbeat_interval',    30));
  setVal('cfgSafetyDiscoTo',       _cfgGet(c, 'safety.disconnect_timeout',   600));
  setVal('cfgSafetyReconAttempts', _cfgGet(c, 'safety.reconnect_attempts',     3));
  setVal('cfgSafetyReconDelay',    _cfgGet(c, 'safety.reconnect_delay',       10));
  setVal('cfgPhotEnabled',     _cfgGet(c, 'photometry.enabled',      false));
  setVal('cfgPhotNodeId',      _cfgGet(c, 'photometry.node_id',       ''));
  setVal('cfgPhotFilter',      _cfgGet(c, 'photometry.filter_name',   ''));
  setVal('cfgPhotTargetName',  _cfgGet(c, 'photometry.target.name',   ''));
  setVal('cfgPhotTargetRA',    _cfgGet(c, 'photometry.target.ra_deg',  ''));
  setVal('cfgPhotTargetDec',   _cfgGet(c, 'photometry.target.dec_deg', ''));
  setVal('cfgPhotAstap',       _cfgGet(c, 'photometry.astap_path',    'astap'));
  setVal('cfgPhotAstapRadius', _cfgGet(c, 'photometry.astap_search_radius', 10));
  setVal('cfgPhotAperture',    _cfgGet(c, 'photometry.aperture_factor', 2.5));
  setVal('cfgPhotAnnulusIn',   _cfgGet(c, 'photometry.annulus_inner',   4.0));
  setVal('cfgPhotAnnulusOut',  _cfgGet(c, 'photometry.annulus_outer',   6.0));
  setVal('cfgPhotFieldRadius', _cfgGet(c, 'photometry.field_radius',    0.5));
  setVal('cfgPhotMagLimit',    _cfgGet(c, 'photometry.mag_limit',      15.0));
  setVal('cfgPhotMinComp',     _cfgGet(c, 'photometry.min_comparison_stars', 3));
  setVal('cfgPhotSNR',         _cfgGet(c, 'photometry.snr_threshold',   20));
  setVal('cfgPhotMaxUnc',      _cfgGet(c, 'photometry.max_uncertainty',  0.3));
  setVal('cfgPhotMaxAirmass',  _cfgGet(c, 'photometry.max_airmass',      3.0));
  setVal('cfgAavsoCode',       _cfgGet(c, 'aavso.observer_code',        ''));
  setVal('cfgAavsoUser',       _cfgGet(c, 'aavso.username',             ''));
  setVal('cfgAavsoPass',       _cfgGet(c, 'aavso.password',             ''));
  setVal('cfgAavsoChartId',    _cfgGet(c, 'aavso.chart_id',             ''));
  setVal('cfgAavsoAuditDir',   _cfgGet(c, 'aavso.audit_dir', 'aavso_submissions'));
  setVal('cfgAavsosDryRun',    _cfgGet(c, 'aavso.dry_run',              false));
  setVal('cfgAavsoSubmitPoor', _cfgGet(c, 'aavso.submit_poor_quality',  false));
  setVal('cfgPierEnabled',     _cfgGet(c, 'pier_cam.enabled',       false));
  setVal('cfgPierDevIdx',      _cfgGet(c, 'pier_cam.device_index',  0));
  setVal('cfgPierExpMs',       _cfgGet(c, 'pier_cam.exposure_ms',   80));
  setVal('cfgPierGain',        _cfgGet(c, 'pier_cam.gain',          200));
  setVal('cfgPierBin',         _cfgGet(c, 'pier_cam.bin',           2));
  setVal('cfgPierFps',         _cfgGet(c, 'pier_cam.target_fps',    10));
  setVal('cfgPierJpegQ',       _cfgGet(c, 'pier_cam.jpeg_quality',  75));
  setVal('cfgPierSdkLib',      _cfgGet(c, 'pier_cam.sdk_lib',       ''));
  setVal('cfgIwEnabled',       _cfgGet(c, 'image_watcher.enabled',        false));
  setVal('cfgIwPath',          _cfgGet(c, 'image_watcher.watch_path', '/mnt/seestar'));
  setVal('cfgIwDebounce',      _cfgGet(c, 'image_watcher.debounce_delay', 2.0));
  setVal('cfgLogLevel',        _cfgGet(c, 'logging.level', 'INFO'));
}

function collectCfgForm() {
  const c = JSON.parse(JSON.stringify(_cfgParsed || {}));
  function set(path, val) {
    const parts = path.split('.');
    let cur = c;
    for (let i = 0; i < parts.length - 1; i++) {
      if (cur[parts[i]] == null || typeof cur[parts[i]] !== 'object') cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = val;
  }
  function num(id, isInt) {
    const v = document.getElementById(id)?.value;
    if (v === '' || v == null) return null;
    return isInt ? parseInt(v, 10) : parseFloat(v);
  }
  function nullNum(id) {
    const v = document.getElementById(id)?.value;
    return (v === '' || v == null) ? null : parseFloat(v);
  }
  function txt(id) { return document.getElementById(id)?.value ?? ''; }
  function chk(id) { return document.getElementById(id)?.checked ?? false; }
  function sel(id) { return document.getElementById(id)?.value ?? ''; }
  set('alpaca.discovery_port',    num('cfgAlpacaPort',    true));
  set('alpaca.discovery_timeout', num('cfgAlpacaTimeout', true));
  set('alpaca.api_version',       num('cfgAlpacaApiVer',  true));
  set('safety.observer.latitude',  num('cfgObsLat'));
  set('safety.observer.longitude', num('cfgObsLon'));
  set('devices.telescope.enabled',         chk('cfgDevTelEnabled'));
  set('devices.telescope.device_number',   num('cfgDevTelNum', true));
  set('devices.camera.enabled',            chk('cfgDevCamEnabled'));
  set('devices.camera.device_number',      num('cfgDevCamNum', true));
  set('devices.focuser.enabled',           chk('cfgDevFocEnabled'));
  set('devices.focuser.device_number',     num('cfgDevFocNum', true));
  set('devices.filterwheel.enabled',       chk('cfgDevFwEnabled'));
  set('devices.filterwheel.device_number', num('cfgDevFwNum', true));
  set('telescope.tracking_rate',   num('cfgTrackingRate', true));
  set('camera.exposure_duration',  num('cfgCamExposure'));
  set('camera.binning',            num('cfgCamBinning', true));
  set('safety.enabled',              chk('cfgSafetyEnabled'));
  set('safety.park_at_dawn',         chk('cfgSafetyParkDawn'));
  set('safety.dawn_type',            sel('cfgSafetyDawnType'));
  set('safety.heartbeat_interval',   num('cfgSafetyHb',            true));
  set('safety.disconnect_timeout',   num('cfgSafetyDiscoTo',       true));
  set('safety.reconnect_attempts',   num('cfgSafetyReconAttempts', true));
  set('safety.reconnect_delay',      num('cfgSafetyReconDelay',    true));
  set('photometry.enabled',               chk('cfgPhotEnabled'));
  set('photometry.node_id',               txt('cfgPhotNodeId'));
  set('photometry.filter_name',           txt('cfgPhotFilter'));
  set('photometry.target.name',           txt('cfgPhotTargetName'));
  set('photometry.target.ra_deg',         nullNum('cfgPhotTargetRA'));
  set('photometry.target.dec_deg',        nullNum('cfgPhotTargetDec'));
  set('photometry.astap_path',            txt('cfgPhotAstap'));
  set('photometry.astap_search_radius',   num('cfgPhotAstapRadius', true));
  set('photometry.aperture_factor',       num('cfgPhotAperture'));
  set('photometry.annulus_inner',         num('cfgPhotAnnulusIn'));
  set('photometry.annulus_outer',         num('cfgPhotAnnulusOut'));
  set('photometry.field_radius',          num('cfgPhotFieldRadius'));
  set('photometry.mag_limit',             num('cfgPhotMagLimit'));
  set('photometry.min_comparison_stars',  num('cfgPhotMinComp', true));
  set('photometry.snr_threshold',         num('cfgPhotSNR', true));
  set('photometry.max_uncertainty',       num('cfgPhotMaxUnc'));
  set('photometry.max_airmass',           num('cfgPhotMaxAirmass'));
  set('aavso.observer_code',       txt('cfgAavsoCode'));
  set('aavso.username',            txt('cfgAavsoUser'));
  set('aavso.password',            txt('cfgAavsoPass'));
  set('aavso.chart_id',            txt('cfgAavsoChartId'));
  set('aavso.audit_dir',           txt('cfgAavsoAuditDir'));
  set('aavso.dry_run',             chk('cfgAavsosDryRun'));
  set('aavso.submit_poor_quality', chk('cfgAavsoSubmitPoor'));
  set('pier_cam.enabled',       chk('cfgPierEnabled'));
  set('pier_cam.device_index',  num('cfgPierDevIdx', true));
  set('pier_cam.exposure_ms',   num('cfgPierExpMs',  true));
  set('pier_cam.gain',          num('cfgPierGain',   true));
  set('pier_cam.bin',           num('cfgPierBin',    true));
  set('pier_cam.target_fps',    num('cfgPierFps'));
  set('pier_cam.jpeg_quality',  num('cfgPierJpegQ',  true));
  set('pier_cam.sdk_lib',       txt('cfgPierSdkLib'));
  set('image_watcher.enabled',        chk('cfgIwEnabled'));
  set('image_watcher.watch_path',     txt('cfgIwPath'));
  set('image_watcher.debounce_delay', num('cfgIwDebounce'));
  set('logging.level', sel('cfgLogLevel'));
  return c;
}

async function openConfigModal() {
  const modal   = document.getElementById("cfgModal");
  const errEl   = document.getElementById("cfgError");
  const saveBtn = document.getElementById("btnCfgSave");
  errEl.textContent   = "";
  saveBtn.disabled    = false;
  saveBtn.textContent = "Save";
  _cfgView = 'form';
  document.getElementById("cfgViewToggle").textContent = "RAW YAML";
  document.getElementById("cfgTabs").style.display     = 'flex';
  document.getElementById("cfgFormView").style.display = '';
  document.getElementById("cfgTextarea").style.display = 'none';
  modal.classList.remove("hidden");
  switchCfgTab(_cfgActiveTab);
  try {
    const r = await fetch("/api/config/parsed");
    _cfgParsed = await r.json();
    renderCfgForm(_cfgParsed);
  } catch (e) {
    errEl.textContent = "Failed to load config: " + e.message;
  }
}

function closeConfigModal() {
  document.getElementById("cfgModal").classList.add("hidden");
}

async function toggleCfgView() {
  const toggle   = document.getElementById("cfgViewToggle");
  const tabs     = document.getElementById("cfgTabs");
  const formView = document.getElementById("cfgFormView");
  const rawView  = document.getElementById("cfgTextarea");
  const errEl    = document.getElementById("cfgError");
  errEl.textContent = "";
  if (_cfgView === 'form') {
    _cfgView = 'raw';
    toggle.textContent     = "Form View";
    tabs.style.display     = 'none';
    formView.style.display = 'none';
    rawView.style.display  = '';
    rawView.value = "Loading…";
    try {
      const r = await fetch("/api/config");
      rawView.value = await r.text();
      rawView.focus(); rawView.setSelectionRange(0, 0); rawView.scrollTop = 0;
    } catch (e) {
      rawView.value = ""; errEl.textContent = "Failed to load config: " + e.message;
    }
  } else {
    _cfgView = 'form';
    toggle.textContent     = "RAW YAML";
    tabs.style.display     = 'flex';
    formView.style.display = '';
    rawView.style.display  = 'none';
    try {
      const r = await fetch("/api/config/parsed");
      _cfgParsed = await r.json();
      renderCfgForm(_cfgParsed);
    } catch (e) { errEl.textContent = "Failed to reload config: " + e.message; }
  }
}

async function saveConfig() {
  const errEl   = document.getElementById("cfgError");
  const saveBtn = document.getElementById("btnCfgSave");
  errEl.textContent   = "";
  saveBtn.disabled    = true;
  saveBtn.textContent = "Saving…";
  try {
    let r;
    if (_cfgView === 'raw') {
      r = await fetch("/api/config", {
        method:  "POST",
        headers: { "Content-Type": "text/plain" },
        body:    document.getElementById("cfgTextarea").value,
      });
    } else {
      const data = collectCfgForm();
      r = await fetch("/api/config/parsed", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(data),
      });
      if (r.ok) _cfgParsed = data;
    }
    if (r.ok) {
      saveBtn.textContent = "Saved ✓";
      setTimeout(() => { saveBtn.textContent = "Save"; saveBtn.disabled = false; }, 1800);
    } else {
      const d = await r.json().catch(() => ({}));
      errEl.textContent   = "Error: " + (d.error || r.statusText);
      saveBtn.textContent = "Save"; saveBtn.disabled = false;
    }
  } catch (e) {
    errEl.textContent   = "Request failed: " + e.message;
    saveBtn.textContent = "Save"; saveBtn.disabled = false;
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
