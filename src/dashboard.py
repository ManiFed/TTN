#!/usr/bin/env python3
"""
NODE v1 — Interactive ALPACA control panel.

Run:  python dashboard.py
Then open http://localhost:5173 in a browser.
"""

import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))

import base64
import copy
import io
import json
import logging
import os
import pathlib
import platform
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Optional

import yaml
from flask import Flask, Response, jsonify, render_template_string, request, send_from_directory, stream_with_context

from pyongc.ongc import listObjects as _ongc_list
from alpaca.discovery import discover_servers
from alpaca.safety_manager import SafetyManager
from alpaca.telescope import Telescope
from alpaca.camera import Camera, ExposureCancelled
from alpaca.focuser import Focuser
from alpaca.autofocus import autofocus_device, AutofocusCancelled, AutofocusError
from alpaca.covercalibrator import CoverCalibrator, COVER_STATE_NAMES, COVER_NOT_PRESENT, COVER_MOVING, COVER_OPEN, COVER_CLOSED
from src.image_watcher import ImageWatcher
from src.photometry import run_pipeline as _run_photometry
from src.aavso_submission import submit as _aavso_submit
from src.fits_export import export_enhanced_fits as _export_fits
from src.geolocation import enrich_config_with_location
from src.telescope_specs import enrich_config_with_telescope
from src.stacking import LiveStacker
from src.cloud_communicator import CloudCommunicator


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
        "arm_state": None,      # CoverCalibrator cover state int, or None if unavailable
        "arm_busy":  False,
    },
    "camera": {
        "enabled":          False,
        "connected":        False,
        "state":            None,
        "state_name":       None,
        "image_ready":      None,
        "exposing":         False,
        "exposure_start_ts": None,
        "exposure_duration": None,
    },
    "focuser": {
        "enabled":   False,
        "connected": False,
        "position":  None,
        "moving":    False,
        "autofocus_running": False,
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
        "queued":      0,      # FITS paths waiting in the photometry queue
    },
    "aavso": {
        "last_submission": None,   # most recent submit() result dict
        "recent_submissions": {},  # {target_name: bjd} — dedup gate
    },
}
_state_lock = threading.Lock()

# Bounded FIFO for photometry jobs.  A single worker thread drains this queue
# so rapid captures don't race each other and multi-frame sequences don't lose
# measurements while the previous plate-solve is still running.
_PHOT_QUEUE_MAX = 50
_phot_queue: queue.Queue = queue.Queue(maxsize=_PHOT_QUEUE_MAX)


def _enqueue_photometry(fits_path: str) -> None:
    """Submit a FITS file for photometry, dropping it if the queue is full."""
    try:
        _phot_queue.put_nowait(fits_path)
        with _state_lock:
            _state["photometry"]["queued"] = _phot_queue.qsize()
    except queue.Full:
        logger.warning(
            "Photometry queue full (%d items) — dropping %s",
            _PHOT_QUEUE_MAX, os.path.basename(fits_path),
        )


def _phot_worker() -> None:
    """Single daemon thread: process FITS files for photometry one at a time."""
    while True:
        try:
            fits_path = _phot_queue.get(block=True, timeout=1.0)
        except queue.Empty:
            continue
        with _state_lock:
            _state["photometry"]["queued"] = _phot_queue.qsize()
        try:
            _run_photometry_bg(fits_path)
        finally:
            _phot_queue.task_done()

# ── Schedule execution state ───────────────────────────────────────────────────

_sched_lock = threading.Lock()
_sched_state: dict = {
    "running":         False,
    "cancelled":       False,
    "current_idx":    -1,
    "current_target":  "",
    "current_phase":   "",   # waiting | slewing | exposing | done | cancelled
    "current_frame":   0,
    "total_frames":    0,
    "completed":       0,
    "total":           0,
    "error":           None,
}

# ── Image history ─────────────────────────────────────────────────────────────

_img_history: list[dict] = []          # metadata + thumbnail b64
_img_history_lock = threading.Lock()
_img_full: dict[str, str] = {}         # id → full-res b64
_img_full_lock = threading.Lock()
_img_counter = 0
_img_counter_lock = threading.Lock()

# ── Local history persistence ──────────────────────────────────────────────────
_DATA_DIR     = pathlib.Path("data")
_IMAGES_DIR   = _DATA_DIR / "images"
_HISTORY_FILE = _DATA_DIR / "camera_history.json"


def _save_history_to_disk() -> None:
    """Write image metadata + thumbnails to data/camera_history.json."""
    try:
        _DATA_DIR.mkdir(exist_ok=True)
        with _img_history_lock:
            history_copy = list(_img_history)
        with open(_HISTORY_FILE, "w") as _f:
            json.dump({"history": history_copy}, _f, separators=(",", ":"))
    except Exception as _exc:
        logger.warning("Could not save camera history: %s", _exc)


def _save_full_image_to_disk(img_id: str, b64_full: str) -> None:
    """Write a full-resolution image as data/images/{img_id}.png."""
    try:
        _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        with open(_IMAGES_DIR / f"{img_id}.png", "wb") as _f:
            _f.write(base64.b64decode(b64_full))
    except Exception as _exc:
        logger.warning("Could not save image %s: %s", img_id, _exc)


def _delete_image_from_disk(img_id: str) -> None:
    """Remove an evicted image file from disk (best-effort)."""
    try:
        (_IMAGES_DIR / f"{img_id}.png").unlink(missing_ok=True)
    except Exception:
        pass


def _load_history_from_disk() -> None:
    """Restore image history metadata from disk on startup."""
    global _img_counter
    if not _HISTORY_FILE.exists():
        return
    try:
        with open(_HISTORY_FILE) as _f:
            data = json.load(_f)
        entries = data.get("history", [])
        max_n = 0
        for e in entries:
            try:
                n = int(e["id"].split("_")[1])
                if n > max_n:
                    max_n = n
            except (ValueError, IndexError, KeyError):
                pass
        with _img_counter_lock:
            _img_counter = max_n
        with _img_history_lock:
            _img_history.extend(entries)
        logger.info("Restored %d images from local history", len(entries))
    except Exception as _exc:
        logger.warning("Could not load camera history: %s", _exc)


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

_tel:   Optional[Telescope]        = None
_cam:   Optional[Camera]           = None
_cover: Optional[CoverCalibrator]  = None
_foc:   Optional[Focuser]          = None
_last_image_b64: Optional[str] = None
_last_image_lock = threading.Lock()

# Serializes all state-changing device commands (slew/park/expose/etc.) so the
# scheduler, manual UI routes, and horizon scan can't drive the mount/camera
# concurrently.  Read-only polling (the poller loop) and abort/emergency-park
# intentionally do NOT take this lock — they must be able to interrupt.
_device_lock = threading.RLock()

# Set to request cancellation of an in-flight manual exposure.  Cleared at the
# start of each manual exposure.
_expose_cancel = threading.Event()

_pier_cam_frame: Optional[bytes] = None
_pier_cam_frame_lock = threading.Lock()
_pier_cam_pause = threading.Event()
_pier_cam_stop  = threading.Event()


def _capture_image(fits_path: Optional[str] = None,
                   exp_dur: Optional[float] = None) -> Optional[str]:
    """Download the last camera image, store it globally, and return its b64.

    If fits_path is provided, also write a FITS file with the raw (un-stretched)
    pixel data and whatever header fields the camera exposes.  This is used by
    the schedule runner so the photometry pipeline has a science-grade file to
    work with rather than a display-stretched PNG.
    """
    global _last_image_b64
    if _cam is None:
        return None
    try:
        import numpy as np
        from PIL import Image

        logger.info("Downloading image array from camera…")
        raw = _cam.image_array()
        arr_raw = np.array(raw, dtype=np.float32)

        # ASCOM Alpaca returns (W, H) or (3, W, H); reshape to (H, W) or (H, W, 3)
        arr_display = arr_raw.copy()
        if arr_display.ndim == 3 and arr_display.shape[0] in (1, 3):
            arr_display = np.transpose(arr_display, (1, 2, 0))
            if arr_display.shape[2] == 1:
                arr_display = arr_display[:, :, 0]

        # ── Save FITS (raw, un-stretched) ──────────────────────────────────
        if fits_path:
            try:
                from astropy.io import fits as _fits
                import time as _time

                # Seestar Alpaca returns (H, W) directly — no transposition needed
                sci = arr_raw if arr_raw.ndim == 2 else arr_raw[0]

                hdr = _fits.Header()
                hdr["SIMPLE"]   = True
                hdr["BITPIX"]   = -32
                hdr["NAXIS"]    = 2
                hdr["NAXIS1"]   = sci.shape[1]
                hdr["NAXIS2"]   = sci.shape[0]
                hdr["DATE-OBS"] = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime())
                # Pull what we can from the camera device
                if exp_dur is None:
                    with _state_lock:
                        exp_dur = _state["camera"].get("exposure_duration")
                if exp_dur is not None:
                    hdr["EXPTIME"] = float(exp_dur)
                try:
                    hdr["CCD-TEMP"] = _cam.ccd_temperature()
                except Exception:
                    pass
                # Telescope pointing
                with _state_lock:
                    ra  = _state["telescope"].get("ra")
                    dec = _state["telescope"].get("dec")
                if ra is not None:
                    hdr["RA"]  = round(float(ra) * 15.0, 6)   # hours→degrees
                    hdr["DEC"] = round(float(dec), 6)
                hdu = _fits.PrimaryHDU(data=sci.astype(np.float32), header=hdr)
                pathlib.Path(fits_path).parent.mkdir(parents=True, exist_ok=True)
                hdu.writeto(fits_path, overwrite=True)
                logger.info("FITS saved: %s  shape=%s", fits_path, sci.shape)
            except Exception as exc:
                logger.error("FITS save failed: %s", exc)

        # ── Display PNG (stretched for viewing) ───────────────────────────
        mn, mx = float(arr_display.min()), float(arr_display.max())
        if mx > mn:
            arr_display = (arr_display - mn) / (mx - mn) * 255.0
        arr_display = arr_display.clip(0, 255).astype(np.uint8)

        mode = "RGB" if arr_display.ndim == 3 else "L"
        img = Image.fromarray(arr_display, mode=mode)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        with _last_image_lock:
            _last_image_b64 = b64
        with _state_lock:
            _state["image_captured"] = True
            _state["image_id"] += 1
        logger.info("Image stored — %.1f KB PNG", len(b64) * 3 / 4 / 1024)
        return b64
    except Exception as exc:
        logger.error("Image capture failed: %s", exc)
        return None


def _make_thumb(b64_full: str, max_px: int = 220) -> str:
    """Return a base64 PNG thumbnail, falling back to the original on error."""
    try:
        from PIL import Image as _PILImg
        raw = base64.b64decode(b64_full)
        img = _PILImg.open(io.BytesIO(raw))
        img.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64_full


def _store_history_image(
    target: str, exp_dur: float, binning: int,
    frame: int, total: int, b64_full: str,
) -> str:
    """Persist a captured frame in the in-memory history. Returns the image ID."""
    global _img_counter
    with _img_counter_lock:
        _img_counter += 1
        img_id = f"img_{_img_counter}"

    thumb = _make_thumb(b64_full)
    entry = {
        "id":      img_id,
        "target":  target,
        "ts":      time.strftime("%H:%M:%S"),
        "date":    time.strftime("%Y-%m-%d"),
        "exp_dur": round(exp_dur, 2),
        "binning": binning,
        "frame":   frame,
        "total":   total,
        "thumb":   thumb,
    }
    evicted_id: Optional[str] = None
    with _img_history_lock:
        _img_history.append(entry)
        if len(_img_history) > 400:
            evicted_id = _img_history.pop(0)["id"]
            with _img_full_lock:
                _img_full.pop(evicted_id, None)
    with _img_full_lock:
        _img_full[img_id] = b64_full
    _save_full_image_to_disk(img_id, b64_full)
    _save_history_to_disk()
    if evicted_id:
        _delete_image_from_disk(evicted_id)
    return img_id


# ── Image watcher ──────────────────────────────────────────────────────────────

_image_watcher: Optional[ImageWatcher] = None

_SEESTAR_SMB_SHARE = "seestar"


def _try_mount_seestar_smb(host: str) -> Optional[str]:
    """Mount the Seestar's SMB share from *host* and return the mount path.

    Uses mount_smbfs (macOS) or mount -t cifs (Linux).  Guest access, no
    password.  Returns None if the mount fails or the platform is unsupported.
    """
    system = platform.system()
    if system == "Darwin":
        mount_point = "/Volumes/Seestar"
        smb_url     = f"//guest:@{host}/{_SEESTAR_SMB_SHARE}"
        cmd         = ["mount_smbfs", "-N", smb_url, mount_point]
    elif system == "Linux":
        mount_point = "/mnt/seestar"
        smb_url     = f"//{host}/{_SEESTAR_SMB_SHARE}"
        cmd         = ["mount", "-t", "cifs", smb_url, mount_point, "-o", "guest,uid=0,gid=0"]
    else:
        return None

    if os.path.ismount(mount_point):
        logger.debug("Seestar SMB already mounted at %s", mount_point)
        return mount_point

    try:
        os.makedirs(mount_point, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create SMB mount point %s: %s", mount_point, exc)
        return None

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.info("Seestar SMB share mounted at %s", mount_point)
            return mount_point
        logger.warning("SMB mount failed (rc=%d): %s", result.returncode, result.stderr.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("SMB mount error: %s", exc)
    return None


def _start_image_watcher_at(path: str) -> None:
    """(Re)start the ImageWatcher pointed at *path*."""
    global _image_watcher
    cfg           = _load_config()
    debounce      = float(cfg.get("image_watcher", {}).get("debounce_delay", 2.0))
    if _image_watcher is not None:
        _image_watcher.stop()
    _image_watcher = ImageWatcher(path, _on_new_fits, debounce)
    _image_watcher.start()
    with _state_lock:
        _state["image_watcher"]["enabled"]    = True
        _state["image_watcher"]["watch_path"] = path
    logger.info("Image watcher started at %s", path)


def _auto_mount_and_watch(host: str) -> None:
    """After connecting to a Seestar, mount its SMB share and start the watcher."""
    cfg    = _load_config()
    iw_cfg = cfg.get("image_watcher", {})
    if not iw_cfg.get("enabled", False):
        return
    mount_path = _try_mount_seestar_smb(host)
    if mount_path is None:
        return
    current_path = iw_cfg.get("watch_path", "")
    watcher_running = _image_watcher is not None and os.path.isdir(current_path)
    if not watcher_running or current_path != mount_path:
        _start_image_watcher_at(mount_path)


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

    if phot_enabled:
        _enqueue_photometry(path)


def _maybe_aavso_submit(result: dict, cfg: dict) -> dict:
    """Submit to AAVSO unless this target was already submitted recently.

    Repeated submissions of the same target from the same node within a short
    window provide no additional value to AAVSO's database.  The minimum
    interval is configurable via aavso.min_submit_interval_hours (default 2 h).
    """
    target = result.get("target_name", "")
    bjd    = float(result.get("bjd", 0.0))
    min_interval_bjd = float(
        cfg.get("aavso", {}).get("min_submit_interval_hours", 2.0)
    ) / 24.0

    with _state_lock:
        last_bjd = _state["aavso"]["recent_submissions"].get(target)

    if last_bjd is not None and (bjd - last_bjd) < min_interval_bjd:
        gap_min = (bjd - last_bjd) * 1440
        logger.info(
            "AAVSO submission skipped: %s already submitted %.0f min ago "
            "(min interval %.0f min) — increase aavso.min_submit_interval_hours to override",
            target, gap_min, min_interval_bjd * 1440,
        )
        return {
            "status": "skipped", "accepted": 0, "rejected": 0,
            "file_path": None, "response_path": None, "record_path": None,
            "message": f"duplicate suppressed: {target} submitted {gap_min:.0f} min ago",
        }

    sub = _aavso_submit(result, cfg)
    if sub.get("status") not in ("error",):
        with _state_lock:
            _state["aavso"]["recent_submissions"][target] = bjd
    return sub


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
                sub = _maybe_aavso_submit(result, cfg)
                with _state_lock:
                    _state["aavso"]["last_submission"] = sub
                logger.info(
                    "AAVSO submission: status=%s accepted=%d rejected=%d — %s",
                    sub["status"], sub["accepted"], sub["rejected"], sub["message"],
                )
                # Upload the .txt file to the cloud regardless of submission status
                # so the operator can download and email it to observations@aavso.org
                if _cloud is not None and sub.get("file_path"):
                    _cloud.upload_aavso_txt(sub["file_path"])
            if _cloud is not None:
                _cloud.submit_measurement(
                    result, conditions=_cloud_conditions(), fits_path=fits_path)
        else:
            logger.warning("Photometry pipeline returned no result for %s",
                           os.path.basename(fits_path))
    except Exception as exc:
        logger.error("Photometry pipeline crashed: %s", exc)
    finally:
        with _state_lock:
            _state["photometry"]["running"] = False


# ── Cloud communicator ─────────────────────────────────────────────────────────

_cloud: Optional[CloudCommunicator] = None


def _cloud_conditions() -> dict:
    """Local conditions snapshot included with cloud heartbeats."""
    out: dict = {}
    if _safety_mgr is not None:
        try:
            s = _safety_mgr.status()
            out["safe"]   = s.get("safe")
            out["reason"] = s.get("reason", "")
        except Exception:
            pass
    with _sched_lock:
        out["schedule_running"] = _sched_state["running"]
    with _state_lock:
        out["photometry_enabled"] = _state["photometry"]["enabled"]
    return out


def _cloud_telescope_specs() -> dict:
    """Live ALPACA hardware specs for the registration payload.

    Reads aperture/focal length off the connected telescope and pixel
    size/sensor/cooler off the camera, so *any* ALPACA-compatible rig registers
    with its real optics.  Returns {} when no device is connected (the catalog
    entry for the configured model is then used instead)."""
    from src.telescope_specs import detect_telescope_specs
    model = _load_config().get("observatory", {}).get("telescope", "")
    return detect_telescope_specs(_tel, _cam, fallback_model=model)


def _on_cloud_plan(items: list) -> None:
    """A new observation plan arrived from the cloud.  Validate it with the
    same gate as /api/schedule/run; execute it only when auto_run_plans is on
    and no schedule is already running."""
    valid, err = _validate_schedule_items(items)
    if err is not None:
        logger.warning("Cloud plan rejected by validator: %s", err)
        return
    cfg = _load_config()
    if not cfg.get("cloud", {}).get("auto_run_plans", False):
        logger.info("Cloud plan received (%d items) — auto_run_plans is off, "
                    "review it in the dashboard scheduler", len(valid))
        return
    with _sched_lock:
        if _sched_state["running"]:
            logger.info("Cloud plan received but a schedule is already "
                        "running — not starting it")
            return
    threading.Thread(
        target=_run_schedule_bg, args=(valid,),
        daemon=True, name="sched-runner-cloud",
    ).start()
    logger.info("Cloud plan started: %d observations", len(valid))


# ── Safety manager ─────────────────────────────────────────────────────────────

_safety_mgr: Optional[SafetyManager] = None


def _on_safety_unsafe() -> None:
    reason = _safety_mgr.status()["reason"] if _safety_mgr else "unknown"
    # Wind down any in-flight work so the emergency park (which runs lock-free)
    # isn't fighting a scheduled slew/exposure for the device.
    _expose_cancel.set()
    with _sched_lock:
        if _sched_state["running"]:
            _sched_state["cancelled"] = True
    with _state_lock:
        _state["error"] = f"Safety stop: {reason}"
    logger.critical("Safety manager triggered: %s", reason)


# ── Horizon scan state ────────────────────────────────────────────────────────

_scan_lock  = threading.Lock()
_scan_state: dict = {
    "running":       False,
    "cancelled":     False,
    "directions":    [],   # list of 12 dicts: {az, status, horizon_alt, steps, cloud_suspect}
    "result":        None, # [[alt, az], …] on completion
    "error":         None,
    "cloud_warning": False, # >40% of completed directions had 0 stars at top altitude
    "cloud_abort":   False, # scan terminated early due to widespread cloud cover
    "cloud_dirs":    0,     # count of directions flagged as cloud-suspected
}


# ── Autofocus state ───────────────────────────────────────────────────────────

_focus_lock  = threading.Lock()
_focus_state: dict = {
    "running":    False,
    "cancelled":  False,
    "samples":    [],    # list of {position, fwhm} measured so far
    "current":    0,     # 1-based index of position being measured
    "total":      0,     # total positions in the sweep
    "result":     None,  # AutofocusResult.as_dict() on success
    "error":      None,
}


def _run_autofocus_bg(
    exposure_s: float,
    step_size: int,
    steps_per_side: int,
    settle_s: float,
    samples_per_point: int,
    min_position: Optional[int],
    max_position: Optional[int],
) -> None:
    """Background thread: sweep the focuser and drive it to best focus."""
    with _focus_lock:
        _focus_state.update({
            "running": True, "cancelled": False, "samples": [],
            "current": 0, "total": 0, "result": None, "error": None,
        })
    with _state_lock:
        _state["focuser"]["autofocus_running"] = True

    def _cancelled() -> bool:
        with _focus_lock:
            return _focus_state["cancelled"]

    def _progress(sample, index, total) -> None:
        with _focus_lock:
            _focus_state["current"] = index
            _focus_state["total"]   = total
            _focus_state["samples"].append({
                "position": sample.position,
                "fwhm": round(sample.fwhm, 2) if sample.fwhm is not None else None,
            })

    try:
        with _device_lock:
            result = autofocus_device(
                _foc, _cam,
                exposure_s=exposure_s,
                step_size=step_size,
                steps_per_side=steps_per_side,
                settle_s=settle_s,
                samples_per_point=samples_per_point,
                min_position=min_position,
                max_position=max_position,
                cancel_check=_cancelled,
                progress_cb=_progress,
            )
        with _focus_lock:
            _focus_state["result"]  = result.as_dict()
            _focus_state["running"] = False
        with _state_lock:
            _state["focuser"]["position"] = result.best_position
        logger.info("Autofocus finished — best position %d", result.best_position)
    except AutofocusCancelled:
        with _focus_lock:
            _focus_state["error"]   = "cancelled"
            _focus_state["running"] = False
        logger.warning("Autofocus cancelled by user")
    except (AutofocusError, Exception) as exc:
        with _focus_lock:
            _focus_state["error"]   = str(exc)
            _focus_state["running"] = False
        logger.error("Autofocus failed: %s", exc)
    finally:
        with _state_lock:
            _state["focuser"]["autofocus_running"] = False


# ── Auto-centering (plate-solve goto refinement) state ────────────────────────

_center_lock  = threading.Lock()
_center_state: dict = {
    "running":    False,
    "cancelled":  False,
    "target_ra":  None,   # degrees
    "target_dec": None,   # degrees
    "iterations": [],     # list of CenterIteration dicts
    "result":     None,   # CenterResult.as_dict() on completion
    "error":      None,
}


def _run_centering_bg(
    target_ra_deg: float,
    target_dec_deg: float,
    exposure_s: float,
    tolerance_arcmin: float,
    max_iterations: int,
    settle_s: float,
) -> None:
    """Background thread: slew → plate-solve → correct until target is centered."""
    from alpaca.platesolve import center_on_target_device, CenteringCancelled, CenteringError

    cfg      = _load_config()
    phot     = cfg.get("photometry", {}) or {}
    astap    = phot.get("astap_path", "astap")
    radius   = float(phot.get("astap_search_radius", 10))

    with _center_lock:
        _center_state.update({
            "running": True, "cancelled": False,
            "target_ra": target_ra_deg, "target_dec": target_dec_deg,
            "iterations": [], "result": None, "error": None,
        })

    def _cancelled() -> bool:
        with _center_lock:
            return _center_state["cancelled"]

    def _progress(it) -> None:
        with _center_lock:
            _center_state["iterations"].append({
                "iteration": it.iteration,
                "commanded_ra": round(it.commanded_ra, 5),
                "commanded_dec": round(it.commanded_dec, 5),
                "solved_ra": round(it.solved_ra, 5) if it.solved_ra is not None else None,
                "solved_dec": round(it.solved_dec, 5) if it.solved_dec is not None else None,
                "error_arcmin": round(it.error_arcmin, 3) if it.error_arcmin is not None else None,
            })

    try:
        with _device_lock:
            result = center_on_target_device(
                _tel, _cam, target_ra_deg, target_dec_deg,
                exposure_s=exposure_s,
                tolerance_arcmin=tolerance_arcmin,
                max_iterations=max_iterations,
                settle_s=settle_s,
                astap_path=astap,
                search_radius=radius,
                cancel_check=_cancelled,
                progress_cb=_progress,
            )
        with _center_lock:
            _center_state["result"]  = result.as_dict()
            _center_state["running"] = False
        if result.success:
            logger.info("Auto-centering succeeded — target centered within %.2f′",
                        result.error_arcmin)
        else:
            logger.warning("Auto-centering finished without reaching tolerance")
    except CenteringCancelled:
        with _center_lock:
            _center_state["error"]   = "cancelled"
            _center_state["running"] = False
        logger.warning("Auto-centering cancelled by user")
    except (CenteringError, Exception) as exc:
        with _center_lock:
            _center_state["error"]   = str(exc)
            _center_state["running"] = False
        logger.error("Auto-centering failed: %s", exc)


# ── Live stacking state ───────────────────────────────────────────────────────

_stack_lock  = threading.Lock()
_stacker: Optional[LiveStacker] = None
_stack_preview_b64: Optional[str] = None
_stack_state: dict = {
    "running":         False,
    "cancelled":       False,
    "frames_target":   0,
    "frames_stacked":  0,
    "frames_total":    0,
    "frames_rejected": 0,
    "last_offset":     [0.0, 0.0],
    "snr_gain":        0.0,
    "error":           None,
    "finished":        False,
}


def _run_stacking_bg(n_frames: int, exposure_s: float, preview_every: int) -> None:
    """Background thread: capture N sub-frames and live-stack them into a preview."""
    global _stacker, _stack_preview_b64

    stacker = LiveStacker()
    with _stack_lock:
        _stacker = stacker
        _stack_preview_b64 = None
        _stack_state.update({
            "running": True, "cancelled": False, "frames_target": n_frames,
            "frames_stacked": 0, "frames_total": 0, "frames_rejected": 0,
            "last_offset": [0.0, 0.0], "snr_gain": 0.0,
            "error": None, "finished": False,
        })

    def _cancelled() -> bool:
        with _stack_lock:
            return _stack_state["cancelled"]

    try:
        for i in range(n_frames):
            if _cancelled():
                logger.info("Live stacking cancelled after %d frames", stacker.frames_stacked)
                break
            try:
                with _device_lock:
                    _cam.expose(exposure_s, readout_timeout=60.0, cancel_check=_cancelled)
                    img = _cam.image_array()
            except ExposureCancelled:
                logger.info("Live stacking: exposure cancelled")
                break
            except Exception as exc:
                logger.error("Live stacking: frame %d capture failed: %s", i + 1, exc)
                continue

            info = stacker.add_frame(img)
            logger.info("Live stacking: frame %d/%d — %s (stacked=%d offset=%s)",
                        i + 1, n_frames, info["reason"], info["frames_stacked"],
                        info["offset"])

            # Refresh the preview periodically (and on the final frame).
            if stacker.frames_stacked and (
                stacker.frames_stacked % max(1, preview_every) == 0 or i == n_frames - 1
            ):
                try:
                    png = stacker.preview_png_b64()
                except Exception as exc:
                    png = None
                    logger.warning("Live stacking: preview render failed: %s", exc)
                with _stack_lock:
                    if png:
                        _stack_preview_b64 = png

            with _stack_lock:
                _stack_state.update({
                    "frames_stacked":  stacker.frames_stacked,
                    "frames_total":    stacker.frames_total,
                    "frames_rejected": stacker.frames_rejected,
                    "last_offset":     list(info["offset"]),
                    "snr_gain":        round(stacker.snr_gain(), 2),
                })

        # Final preview render so the UI always ends on the best image.
        if stacker.frames_stacked:
            try:
                png = stacker.preview_png_b64()
                if png:
                    with _stack_lock:
                        _stack_preview_b64 = png
            except Exception:
                pass
        with _stack_lock:
            _stack_state["running"]  = False
            _stack_state["finished"] = True
        logger.info("Live stacking complete — %d frames stacked (SNR gain ~%.1f×)",
                    stacker.frames_stacked, stacker.snr_gain())
    except Exception as exc:
        with _stack_lock:
            _stack_state["error"]   = str(exc)
            _stack_state["running"] = False
        logger.error("Live stacking crashed: %s", exc)


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


def _wait_slew_complete(timeout: float = 120.0) -> bool:
    """Block until the telescope stops slewing.

    Returns True if the mount reported Slewing=False before the timeout,
    False if it timed out (caller should treat the pointing as unreliable).
    """
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
                return True
        except Exception:
            pass
        time.sleep(0.5)
    logger.warning("Slew did not complete within %.0f s", timeout)
    return False


def _slew_rejection(ra_h: float, dec_d: float) -> Optional[str]:
    """Return a human-readable reason a RA/Dec slew should be refused, or None.

    Gates on the SafetyManager's overall safe state and on the configured
    horizon mask.  Used by both the manual slew route and the scheduler so the
    two paths enforce identical safety rules.
    """
    if _safety_mgr is not None and not _safety_mgr.is_safe():
        reason = _safety_mgr.status().get("reason") or "unknown"
        return f"system is in an unsafe state ({reason})"

    if _safety_mgr is not None and _safety_mgr._horizon_mask:
        cfg = _load_config()
        obs = cfg.get("safety", {}).get("observer", {})
        lat = float(obs.get("latitude", 0.0))
        lon = float(obs.get("longitude", 0.0))
        if lat != 0.0 or lon != 0.0:
            try:
                from astropy.coordinates import AltAz, EarthLocation, SkyCoord
                from astropy.time import Time
                import astropy.units as u
                loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)
                frame = AltAz(obstime=Time.now(), location=loc)
                coord = SkyCoord(ra=ra_h * 15.0 * u.deg, dec=dec_d * u.deg).transform_to(frame)
                alt, az = float(coord.alt.deg), float(coord.az.deg)
                if not _safety_mgr.is_pointing_safe(alt, az):
                    min_alt = _safety_mgr.min_safe_altitude(az)
                    return (f"horizon mask: Alt {alt:.1f}° is below the "
                            f"{min_alt:.1f}° limit at Az {az:.1f}°")
            except Exception as exc:
                logger.debug("Horizon-mask RA/Dec check skipped: %s", exc)
    return None


def _scan_slew_to(alt: float, az: float) -> None:
    """
    Slew to an Alt/Az position for the horizon scan.
    Tries native Alt/Az first; falls back to RA/Dec conversion via astropy.
    Blocks until the slew completes.
    """
    try:
        with _device_lock:
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
    with _device_lock:
        _tel.slew_to_coordinates(float(eq.ra.deg) / 15.0, float(eq.dec.deg))


def _run_horizon_scan(
    floor_alt: float,
    start_alt: float,
    step_deg:  float,
    exposure_s: float,
    star_threshold: int,
    settle_s: float,
) -> None:
    """Background thread: slew to 12 azimuths × N altitudes, count stars, build mask.

    Cloud detection: structural obstructions (trees, buildings) only block low
    altitudes.  If the topmost test position for a spoke already shows zero stars,
    that reading is a cloud suspect rather than a true horizon.  When >67% of
    completed spokes are cloud-suspected the scan aborts early; >40% sets a
    warning flag so the user knows to treat the result with caution.
    """
    import numpy as np

    N       = 12
    AZ_STEP = 30

    # Fraction thresholds for cloud detection (fraction of completed directions)
    CLOUD_WARN_FRAC  = 0.40   # warn when this fraction of spokes suspect clouds
    CLOUD_ABORT_FRAC = 0.67   # abort when this fraction of spokes suspect clouds

    directions = [
        {"az": i * AZ_STEP, "status": "pending", "horizon_alt": None,
         "steps": [], "cloud_suspect": False}
        for i in range(N)
    ]
    with _scan_lock:
        _scan_state.update({
            "running":       True,
            "cancelled":     False,
            "directions":    directions,
            "result":        None,
            "error":         None,
            "cloud_warning": False,
            "cloud_abort":   False,
            "cloud_dirs":    0,
        })

    result_alts: list[float] = []
    cloud_dir_count = 0   # directions whose top altitude had < star_threshold stars

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
            top_alt_stars: Optional[int]    = None   # stars at highest tested altitude

            for alt_idx, alt in enumerate(altitudes):
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
                    with _device_lock:
                        _cam.expose(exposure_s, readout_timeout=60.0)
                        img = _cam.image_array()
                    stars = _count_stars_in_array(img)
                    logger.info("Scan: Alt=%.1f Az=%.1f → %d stars", alt, az, stars)
                except Exception as exc:
                    logger.error("Scan: exposure at Alt=%.1f Az=%.1f failed: %s", alt, az, exc)

                # Record star count at the highest tested altitude for this spoke
                if alt_idx == 0 and stars is not None:
                    top_alt_stars = stars

                with _scan_lock:
                    _scan_state["directions"][i]["steps"].append(
                        {"alt": alt, "stars": stars}
                    )

                if stars is not None and stars >= star_threshold:
                    last_clear_alt = alt
                elif last_clear_alt is not None:
                    # Transition found: had sky, now blocked → stop descending.
                    # This is the real horizon for this azimuth — don't keep going.
                    break

            # ── Cloud detection for this direction ────────────────────────────
            # A structural obstruction only blocks low altitude.  If the top
            # altitude already had zero stars AND we never found clear sky, that's
            # a cloud signal, not a permanent obstruction.
            cloud_suspect = (
                top_alt_stars is not None
                and top_alt_stars < star_threshold
                and last_clear_alt is None
            )
            if cloud_suspect:
                cloud_dir_count += 1
                logger.warning(
                    "Scan: Az=%.0f° — 0 stars at %.1f° (cloud cover suspected, not obstruction)",
                    az, start_alt,
                )

            with _scan_lock:
                _scan_state["directions"][i]["cloud_suspect"] = cloud_suspect
                _scan_state["cloud_dirs"] = cloud_dir_count

            # ── Rolling cloud-abort check (after ≥3 directions) ──────────────
            dirs_done = i + 1
            if dirs_done >= 3:
                cloud_frac = cloud_dir_count / dirs_done
                if cloud_frac > CLOUD_ABORT_FRAC:
                    logger.error(
                        "Scan: aborting — %d/%d directions show no stars at %.1f° "
                        "(widespread cloud cover suspected)",
                        cloud_dir_count, dirs_done, start_alt,
                    )
                    with _scan_lock:
                        _scan_state["cloud_warning"] = True
                        _scan_state["cloud_abort"]   = True
                        _scan_state["error"] = (
                            f"{cloud_dir_count}/{dirs_done} directions showed no stars at "
                            f"{start_alt:.0f}° — clouds suspected. "
                            "Wait for a clear night and try again."
                        )
                        _scan_state["running"] = False
                    return
                elif cloud_frac > CLOUD_WARN_FRAC:
                    with _scan_lock:
                        _scan_state["cloud_warning"] = True

            # ── Derive horizon altitude for this azimuth ──────────────────────
            if last_clear_alt is None:
                # Never saw sky — could be full obstruction OR cloud.
                # Store start_alt as a conservative limit; the cloud_suspect flag
                # tells the UI not to trust this reading.
                horizon_alt = round(start_alt, 1)
            elif last_clear_alt <= floor_alt + 1e-6:
                # Still clear at the hardware floor — real horizon is below our range
                horizon_alt = 0.0
            else:
                horizon_alt = round(last_clear_alt, 1)

            result_alts.append(horizon_alt)
            with _scan_lock:
                _scan_state["directions"][i]["status"]      = "done"
                _scan_state["directions"][i]["horizon_alt"] = horizon_alt

        # Final cloud-warning pass over all completed directions
        if cloud_dir_count / N > CLOUD_WARN_FRAC:
            with _scan_lock:
                _scan_state["cloud_warning"] = True

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


_HEARTBEAT_INTERVAL = 300  # seconds between "all good" heartbeat log lines


def _emit_heartbeat() -> None:
    """Log a friendly status summary when all systems are nominal."""
    with _state_lock:
        tel  = _state["telescope"]
        cam  = _state["camera"]
        safe = _state["safety"]

    parts: list[str] = []

    if tel.get("connected"):
        ra      = tel.get("ra")
        dec     = tel.get("dec")
        parked  = tel.get("parked", False)
        slewing = tel.get("slewing", False)
        tracking = tel.get("tracking", False)
        if parked:
            tel_status = "parked"
        elif slewing:
            tel_status = "slewing"
        elif tracking:
            tel_status = "tracking"
        else:
            tel_status = "idle"
        if ra is not None and dec is not None:
            parts.append(f"telescope {tel_status} RA={ra:.4f}h Dec={dec:+.2f}°")
        else:
            parts.append(f"telescope {tel_status}")

    if cam.get("connected"):
        cam_name = cam.get("state_name", "Ready")
        parts.append(f"camera {cam_name.lower()}")

    if not parts:
        return  # nothing connected — skip heartbeat

    sun_el = safe.get("sun_elevation")
    if sun_el is not None:
        parts.append(f"sun {sun_el:+.1f}°")

    with _sched_lock:
        sched_running = _sched_state.get("running", False)
        sched_target  = _sched_state.get("current_target", "")
        sched_frame   = _sched_state.get("current_frame", 0)
        sched_total   = _sched_state.get("total_frames", 0)
        sched_phase   = _sched_state.get("current_phase", "")

    if sched_running and sched_target:
        if sched_phase == "exposing" and sched_total:
            parts.append(f"schedule: {sched_target} frame {sched_frame}/{sched_total}")
        elif sched_phase == "slewing":
            parts.append(f"schedule: slewing to {sched_target}")
        else:
            parts.append(f"schedule: {sched_target}")

    logger.info("Heartbeat — all systems nominal | %s", " | ".join(parts))


def _poll_loop() -> None:
    _tel_connected_prev: Optional[bool] = None
    _cam_connected_prev: Optional[bool] = None
    _heartbeat_ticks = 0

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
                arm_state = None
                if _cover is not None:
                    try:
                        arm_state = _cover.cover_state()
                    except Exception:
                        arm_state = None
                with _state_lock:
                    _state["telescope"].update(
                        connected=True, ra=ra, dec=dec,
                        slewing=slewing, parked=parked, tracking=tracking,
                        arm_state=arm_state,
                    )
                if _tel_connected_prev is False:
                    logger.info("Telescope connection restored")
                _tel_connected_prev = True
            except Exception:
                with _state_lock:
                    _state["telescope"]["connected"] = False
                if _tel_connected_prev is True:
                    logger.warning("Telescope connection lost")
                _tel_connected_prev = False

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
                if _cam_connected_prev is False:
                    logger.info("Camera connection restored")
                _cam_connected_prev = True
            except Exception:
                with _state_lock:
                    _state["camera"]["connected"] = False
                if _cam_connected_prev is True:
                    logger.warning("Camera connection lost")
                _cam_connected_prev = False

        # Poll focuser position only when it isn't mid-autofocus (the sweep holds
        # _device_lock and drives the focuser itself; concurrent reads are skipped
        # to avoid contending with in-progress moves on stricter drivers).
        with _state_lock:
            foc_enabled = _state["focuser"]["enabled"]
            af_running  = _state["focuser"]["autofocus_running"]
        if foc_enabled and _foc is not None and not af_running:
            try:
                pos    = _foc.position()
                moving = _foc.is_moving()
                with _state_lock:
                    _state["focuser"].update(connected=True, position=pos, moving=moving)
            except Exception:
                with _state_lock:
                    _state["focuser"]["connected"] = False

        if _safety_mgr is not None:
            try:
                safety_snap = _safety_mgr.status()
                with _state_lock:
                    _state["safety"].update(safety_snap)
            except Exception:
                pass

        _heartbeat_ticks += 1
        if _heartbeat_ticks >= _HEARTBEAT_INTERVAL:
            _heartbeat_ticks = 0
            _emit_heartbeat()

        time.sleep(1.0)


def _start_poller() -> None:
    _poller_stop.clear()
    t = threading.Thread(target=_poll_loop, daemon=True, name="alpaca-poller")
    t.start()


# ── Config helper ──────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        with open("config.yaml") as fh:
            cfg = yaml.safe_load(fh)
    except FileNotFoundError:
        cfg = {}
    cfg = enrich_config_with_location(cfg)
    return enrich_config_with_telescope(cfg)


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
    from flask import abort
    abort(403)


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
    default_srv = alpaca_cfg.get("default_server")
    return jsonify({"servers": servers, "default_server": default_srv})


_SEESTAR_AP_IP = "192.168.4.1"


@app.route("/api/connect", methods=["POST"])
def api_connect():
    global _tel, _cam, _cover, _foc
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

    tel_ok = cam_ok = False
    errors: list[str] = []

    if devices.get("telescope", {}).get("enabled", False):
        num = devices["telescope"].get("device_number", 0)
        try:
            _tel = Telescope(host, port, num, api_ver)
            _tel.connect()
            tel_ok = True
            tel_device_name = ""
            tel_serial = ""
            try:
                from alpaca.discovery import _fetch_device_info
                _dinfo = _fetch_device_info(host, port)
                tel_device_name = _dinfo.get("device_name", "")
                tel_serial = _dinfo.get("serial", "")
            except Exception:
                pass
            with _state_lock:
                _state["telescope"].update(
                    enabled=True, connected=True,
                    device_name=tel_device_name, serial=tel_serial,
                )
            if tel_serial:
                try:
                    cfg_w = _load_config()
                    cfg_w.setdefault("observatory", {})["telescope_serial"] = tel_serial
                    if tel_device_name:
                        cfg_w["observatory"]["telescope_name"] = tel_device_name
                    with open("config.yaml", "w") as fh:
                        yaml.dump(cfg_w, fh, default_flow_style=False,
                                  sort_keys=False, allow_unicode=True)
                    logger.info("Telescope identified: %s (serial: %s)", tel_device_name, tel_serial)
                except Exception as exc:
                    logger.warning("Could not save telescope serial: %s", exc)
            if _safety_mgr is not None:
                _safety_mgr.attach_telescope(_tel)
        except Exception as exc:
            logger.error("Telescope connect failed: %s", exc)
            errors.append(f"telescope: {exc}")
            _tel = None

    if devices.get("camera", {}).get("enabled", False):
        num = devices["camera"].get("device_number", 0)
        try:
            _cam = Camera(host, port, num, api_ver)
            _cam.connect()
            cam_ok = True
            with _state_lock:
                _state["camera"].update(enabled=True, connected=True)
        except Exception as exc:
            logger.error("Camera connect failed: %s", exc)
            errors.append(f"camera: {exc}")
            _cam = None

    if devices.get("focuser", {}).get("enabled", False):
        num = devices["focuser"].get("device_number", 0)
        try:
            _foc = Focuser(host, port, num, api_ver)
            _foc.connect()
            with _state_lock:
                _state["focuser"].update(enabled=True, connected=True,
                                         position=_foc.position())
        except Exception as exc:
            logger.warning("Focuser connect failed (autofocus unavailable): %s", exc)
            _foc = None

    if devices.get("covercalibrator", {}).get("enabled", False):
        num = devices["covercalibrator"].get("device_number", 0)
        try:
            _cover = CoverCalibrator(host, port, num, api_ver)
            _cover.connect()
        except Exception as exc:
            logger.warning("CoverCalibrator connect failed (arm control unavailable): %s", exc)
            _cover = None

    if not (tel_ok or cam_ok):
        with _state_lock:
            _state["connected"] = False
            _state["server"] = None
        return jsonify({"error": "No devices connected — " + "; ".join(errors)}), 502

    with _state_lock:
        _state["connected"] = True

    if bool(data.get("set_as_default", False)):
        try:
            cfg_w = _load_config()
            if "alpaca" not in cfg_w or cfg_w["alpaca"] is None:
                cfg_w["alpaca"] = {}
            cfg_w["alpaca"]["default_server"] = {"address": host, "port": port}
            with open("config.yaml", "w") as fh:
                yaml.dump(cfg_w, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
            logger.info("Default ALPACA server set to %s:%d", host, port)
        except OSError as exc:
            logger.warning("Could not save default server: %s", exc)

    _parts = []
    if tel_ok: _parts.append("telescope")
    if cam_ok: _parts.append("camera")
    logger.info("Connected to %s:%d — %s", host, port, " + ".join(_parts))
    if errors:
        logger.warning("Connection warnings: %s", "; ".join(errors))
    _start_poller()
    threading.Thread(target=_auto_mount_and_watch, args=(host,), daemon=True,
                     name="smb-automount").start()
    return jsonify({"ok": True, "telescope": tel_ok, "camera": cam_ok, "errors": errors})


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    global _tel, _cam, _cover, _foc
    with _state_lock:
        _server = _state.get("server") or {}
    _disc_host = _server.get("address", "")
    _disc_port = _server.get("port", "")
    _poller_stop.set()
    with _state_lock:
        _state["connected"] = False
        _state["telescope"]["connected"] = False
        _state["camera"]["connected"] = False
        _state["focuser"]["connected"] = False
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
    try:
        if _foc is not None:
            _foc.disconnect()
    except Exception:
        pass
    try:
        if _cover is not None:
            _cover.disconnect()
    except Exception:
        pass
    _tel   = None
    _cam   = None
    _cover = None
    _foc   = None
    with _state_lock:
        _state["telescope"]["arm_state"] = None
        _state["telescope"]["arm_busy"]  = False
    if _disc_host:
        logger.info("Disconnected from %s:%s", _disc_host, _disc_port)
    else:
        logger.info("Disconnected from ALPACA server")
    return jsonify({"ok": True})


@app.route("/api/telescope/unpark", methods=["POST"])
def api_unpark():
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400

    def _do():
        with _state_lock:
            _state["telescope"]["busy"] = True
        try:
            with _device_lock:
                _tel.unpark()
            logger.info("Unpark complete — mount ready")
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
            with _device_lock:
                _tel.park()
            logger.info("Park complete — mount stowed")
        except Exception as exc:
            logger.error("Park failed: %s", exc)
        finally:
            with _state_lock:
                _state["telescope"]["busy"] = False

    threading.Thread(target=_do, daemon=True, name="tel-park").start()
    logger.info("Park commanded")
    return jsonify({"ok": True})


@app.route("/api/arm/open", methods=["POST"])
def api_arm_open():
    if _cover is None:
        return jsonify({"error": "CoverCalibrator not connected"}), 400

    def _do():
        with _state_lock:
            _state["telescope"]["arm_busy"] = True
        try:
            _cover.open_cover()
            logger.info("Arm open commanded")
        except Exception as exc:
            logger.error("Arm open failed: %s", exc)
        finally:
            with _state_lock:
                _state["telescope"]["arm_busy"] = False

    threading.Thread(target=_do, daemon=True, name="arm-open").start()
    return jsonify({"ok": True})


@app.route("/api/arm/close", methods=["POST"])
def api_arm_close():
    if _cover is None:
        return jsonify({"error": "CoverCalibrator not connected"}), 400

    def _do():
        with _state_lock:
            _state["telescope"]["arm_busy"] = True
        try:
            _cover.close_cover()
            logger.info("Arm close commanded")
        except Exception as exc:
            logger.error("Arm close failed: %s", exc)
        finally:
            with _state_lock:
                _state["telescope"]["arm_busy"] = False

    threading.Thread(target=_do, daemon=True, name="arm-close").start()
    return jsonify({"ok": True})


@app.route("/api/telescope/tracking", methods=["POST"])
def api_tracking():
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400
    data    = request.get_json(force=True) or {}
    enabled = bool(data.get("enabled", True))
    try:
        with _device_lock:
            _tel.set_tracking(enabled)
    except Exception as exc:
        logger.error("Set tracking failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    logger.info("Tracking %s", "enabled" if enabled else "disabled")
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

        force = bool(data.get("force", False))

        # Safety gate — refuse to move while the system is unsafe
        if _safety_mgr is not None and not _safety_mgr.is_safe():
            reason = _safety_mgr.status().get("reason") or "unknown"
            msg = f"Slew rejected — system is in an unsafe state ({reason})"
            if not force:
                logger.warning(msg)
                return jsonify({"error": msg, "unsafe": True}), 403
            logger.warning("FORCED slew despite unsafe state: %s", reason)

        # Horizon-mask check
        if _safety_mgr is not None and not _safety_mgr.is_pointing_safe(alt, az):
            min_alt = _safety_mgr.min_safe_altitude(az)
            msg = (
                f"Slew rejected by horizon mask: "
                f"Alt {alt:.1f}° is below the {min_alt:.1f}° limit at Az {az:.1f}°"
            )
            if not force:
                logger.warning(msg)
                return jsonify({"error": msg, "horizon_blocked": True,
                                "min_safe_alt": round(min_alt, 1)}), 403
            logger.warning("FORCED slew past horizon mask: %s", msg)

        logger.info("Slewing: Alt=%.1f°  Az=%.1f°", alt, az)
        try:
            with _device_lock:
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
                with _device_lock:
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

        force = bool(data.get("force", False))

        # Safety + horizon-mask gate (shared with the scheduler)
        rejection = _slew_rejection(ra, dec)
        if rejection is not None:
            msg = f"Slew rejected — {rejection}"
            if not force:
                logger.warning(msg)
                return jsonify({"error": msg, "blocked": True}), 403
            logger.warning("FORCED slew despite rejection: %s", rejection)

        logger.info("Slewing: RA=%.4f h  Dec=%.4f°", ra, dec)
        try:
            with _device_lock:
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

    rejection = _slew_rejection(new_ra, new_dec)
    if rejection is not None:
        msg = f"Nudge rejected — {rejection}"
        logger.warning(msg)
        return jsonify({"error": msg, "blocked": True}), 403

    try:
        with _device_lock:
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
        with _device_lock:
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
            _state["camera"]["exposing"]           = True
            _state["camera"]["exposure_start_ts"]  = time.time()
            _state["camera"]["exposure_duration"]  = duration
            _state["image_captured"]               = False
        _pier_cam_pause.set()
        _expose_cancel.clear()
        time.sleep(0.15)
        try:
            with _device_lock:
                _cam.set_binning(binning)
                _cam.expose(duration=duration, light=True,
                            cancel_check=_expose_cancel.is_set)
                b64 = _capture_image()
            if b64:
                # Grab current telescope position as target label
                with _state_lock:
                    ra  = _state["telescope"].get("ra")
                    dec = _state["telescope"].get("dec")
                target = f"Manual RA {ra:.4f}h" if ra is not None else "Manual"
                logger.info("Exposure complete — image captured (%.2f s  bin%d)", duration, binning)
                _store_history_image(target, duration, binning, 1, 1, b64)
        except ExposureCancelled:
            logger.warning("Manual exposure aborted")
        except Exception as exc:
            logger.error("Exposure failed: %s", exc)
        finally:
            with _state_lock:
                _state["camera"]["exposing"]          = False
                _state["camera"]["exposure_start_ts"] = None
                _state["camera"]["exposure_duration"] = None
            _pier_cam_pause.clear()

    threading.Thread(target=_do, daemon=True, name="cam-expose").start()
    logger.info("Exposure started: %.2f s  binning %dx%d", duration, binning, binning)
    return jsonify({"ok": True})


@app.route("/api/camera/abort", methods=["POST"])
def api_abort_exposure():
    if _cam is None:
        return jsonify({"error": "Camera not connected"}), 400
    # Signal the in-flight exposure (manual or the current scheduled frame) to
    # stop polling, and send abort directly so a long exposure is interrupted
    # immediately.  The abort PUT intentionally bypasses _device_lock — it must
    # preempt.  This stops only the current frame; use /api/schedule/abort to
    # stop a whole run.
    _expose_cancel.set()
    try:
        _cam.abort_exposure()
        with _state_lock:
            _state["camera"]["exposing"] = False
    except Exception as exc:
        logger.error("Abort exposure failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/cloud")
def api_cloud():
    if _cloud is None:
        return jsonify({"enabled": False})
    return jsonify({"enabled": True, **_cloud.status})


@app.route("/api/cloud/connect", methods=["POST"])
def api_cloud_connect():
    """Re-read config and attempt registration immediately (no restart needed)."""
    if _cloud is None:
        return jsonify({"ok": False, "error": "cloud communicator not running"}), 503
    import yaml
    try:
        cfg = yaml.safe_load(open("config.yaml").read()) or {}
        _cloud._config = cfg
        _cloud._url = (cfg.get("cloud") or {}).get("url", _cloud._url) or _cloud._url
    except Exception as exc:
        return jsonify({"ok": False, "error": f"config reload failed: {exc}"}), 500
    ok = _cloud._ensure_registered()
    if ok:
        return jsonify({"ok": True, "registered": True, "node_id": _cloud._node_id})
    return jsonify({"ok": False, "registered": False,
                    "error": _cloud.status.get("error", "registration failed")}), 400


@app.route("/api/safety")
def api_safety():
    if _safety_mgr is None:
        return jsonify({"enabled": False})
    return jsonify({"enabled": True, **_safety_mgr.status()})


@app.route("/api/safety/reset", methods=["POST"])
def api_safety_reset():
    """Manually clear a latched unsafe/parked state so operations can resume."""
    if _safety_mgr is None:
        return jsonify({"error": "Safety manager not enabled"}), 400
    cleared = _safety_mgr.reset()
    logger.info("Safety state reset via API (was: %s)", cleared or "safe")
    return jsonify({"ok": True, "cleared": cleared, **_safety_mgr.status()})


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


# ── Autofocus ─────────────────────────────────────────────────────────────────

@app.route("/api/focus/auto", methods=["POST"])
def api_autofocus_start():
    if _foc is None:
        return jsonify({"error": "Focuser not connected — enable devices.focuser "
                                 "in config.yaml and reconnect"}), 400
    if _cam is None:
        return jsonify({"error": "Camera not connected — autofocus needs it to "
                                 "measure star sharpness"}), 400
    with _focus_lock:
        if _focus_state["running"]:
            return jsonify({"error": "Autofocus already running"}), 409
    with _scan_lock:
        if _scan_state["running"]:
            return jsonify({"error": "Horizon scan in progress — wait for it to finish"}), 409
    with _sched_lock:
        if _sched_state.get("running"):
            return jsonify({"error": "Schedule running — abort it before autofocus"}), 409

    data = request.get_json(force=True) or {}
    cfg  = _load_config().get("autofocus", {}) or {}

    def _num(key, default, cast):
        val = data.get(key, cfg.get(key, default))
        try:
            return cast(val)
        except (TypeError, ValueError):
            return cast(default)

    exposure_s     = _num("exposure_s", 2.0, float)
    step_size      = _num("step_size", 50, int)
    steps_per_side = _num("steps_per_side", 5, int)
    settle_s       = _num("settle_s", 1.0, float)
    samples        = _num("samples_per_point", 1, int)
    min_pos = cfg.get("min_position", data.get("min_position"))
    max_pos = cfg.get("max_position", data.get("max_position"))
    min_pos = int(min_pos) if min_pos is not None else None
    max_pos = int(max_pos) if max_pos is not None else None

    t = threading.Thread(
        target=_run_autofocus_bg,
        args=(exposure_s, step_size, steps_per_side, settle_s, samples, min_pos, max_pos),
        daemon=True,
        name="autofocus",
    )
    t.start()
    logger.info(
        "Autofocus started (exp=%.1fs step=%d ±%d settle=%.1fs samples=%d)",
        exposure_s, step_size, steps_per_side, settle_s, samples,
    )
    return jsonify({"ok": True})


@app.route("/api/focus/auto", methods=["DELETE"])
def api_autofocus_cancel():
    with _focus_lock:
        if not _focus_state["running"]:
            return jsonify({"ok": True, "message": "No autofocus running"})
        _focus_state["cancelled"] = True
    logger.info("Autofocus cancellation requested")
    return jsonify({"ok": True})


@app.route("/api/focus/auto/status", methods=["GET"])
def api_autofocus_status():
    with _focus_lock:
        return jsonify(dict(_focus_state))


# ── Auto-centering (plate-solve goto refinement) ──────────────────────────────

@app.route("/api/center/run", methods=["POST"])
def api_center_start():
    if _tel is None:
        return jsonify({"error": "Telescope not connected"}), 400
    if _cam is None:
        return jsonify({"error": "Camera not connected — needed to plate-solve"}), 400
    with _center_lock:
        if _center_state["running"]:
            return jsonify({"error": "Auto-centering already running"}), 409
    with _scan_lock:
        if _scan_state["running"]:
            return jsonify({"error": "Horizon scan in progress"}), 409
    with _focus_lock:
        if _focus_state["running"]:
            return jsonify({"error": "Autofocus in progress"}), 409
    with _sched_lock:
        if _sched_state.get("running"):
            return jsonify({"error": "Schedule running — abort it first"}), 409

    data = request.get_json(force=True) or {}
    # RA accepted in hours (UI/catalog convention); Dec in degrees.
    try:
        ra_h    = float(data["ra"])
        dec_deg = float(data["dec"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Provide target 'ra' (hours) and 'dec' (degrees)"}), 400
    if not (0.0 <= ra_h < 24.0):
        return jsonify({"error": "RA must be in range [0, 24)"}), 400
    if not (-90.0 <= dec_deg <= 90.0):
        return jsonify({"error": "Dec must be in range [-90, 90]"}), 400

    # Reuse the shared safety + horizon-mask gate before commanding any motion.
    force = bool(data.get("force", False))
    rejection = _slew_rejection(ra_h, dec_deg)
    if rejection is not None and not force:
        logger.warning("Auto-centering rejected — %s", rejection)
        return jsonify({"error": f"Auto-centering rejected — {rejection}",
                        "blocked": True}), 403

    cfg = _load_config().get("centering", {}) or {}

    def _num(key, default, cast):
        try:
            return cast(data.get(key, cfg.get(key, default)))
        except (TypeError, ValueError):
            return cast(default)

    exposure_s = _num("exposure_s", 3.0, float)
    tolerance  = _num("tolerance_arcmin", 3.0, float)
    max_iter   = _num("max_iterations", 4, int)
    settle_s   = _num("settle_s", 2.0, float)

    t = threading.Thread(
        target=_run_centering_bg,
        args=(ra_h * 15.0, dec_deg, exposure_s, tolerance, max_iter, settle_s),
        daemon=True,
        name="auto-center",
    )
    t.start()
    logger.info(
        "Auto-centering started → RA=%.4f h Dec=%.4f° (tol=%.1f′ max_iter=%d exp=%.1fs)",
        ra_h, dec_deg, tolerance, max_iter, exposure_s,
    )
    return jsonify({"ok": True})


@app.route("/api/center/run", methods=["DELETE"])
def api_center_cancel():
    with _center_lock:
        if not _center_state["running"]:
            return jsonify({"ok": True, "message": "No auto-centering running"})
        _center_state["cancelled"] = True
    logger.info("Auto-centering cancellation requested")
    return jsonify({"ok": True})


@app.route("/api/center/status", methods=["GET"])
def api_center_status():
    with _center_lock:
        return jsonify(dict(_center_state))


# ── Live stacking ─────────────────────────────────────────────────────────────

@app.route("/api/stack/start", methods=["POST"])
def api_stack_start():
    if _cam is None:
        return jsonify({"error": "Camera not connected"}), 400
    with _stack_lock:
        if _stack_state["running"]:
            return jsonify({"error": "Live stacking already running"}), 409
    with _focus_lock:
        if _focus_state["running"]:
            return jsonify({"error": "Autofocus in progress"}), 409
    with _center_lock:
        if _center_state["running"]:
            return jsonify({"error": "Auto-centering in progress"}), 409
    with _sched_lock:
        if _sched_state.get("running"):
            return jsonify({"error": "Schedule running — abort it first"}), 409

    data = request.get_json(force=True) or {}
    cfg  = _load_config().get("stacking", {}) or {}

    def _num(key, default, cast):
        try:
            return cast(data.get(key, cfg.get(key, default)))
        except (TypeError, ValueError):
            return cast(default)

    n_frames      = max(1, _num("frames", 20, int))
    exposure_s    = _num("exposure_s", 10.0, float)
    preview_every = max(1, _num("preview_every", 1, int))

    t = threading.Thread(
        target=_run_stacking_bg,
        args=(n_frames, exposure_s, preview_every),
        daemon=True,
        name="live-stack",
    )
    t.start()
    logger.info("Live stacking started — %d × %.1fs frames", n_frames, exposure_s)
    return jsonify({"ok": True})


@app.route("/api/stack/start", methods=["DELETE"])
def api_stack_stop():
    with _stack_lock:
        if not _stack_state["running"]:
            return jsonify({"ok": True, "message": "No live stacking running"})
        _stack_state["cancelled"] = True
    logger.info("Live stacking stop requested")
    return jsonify({"ok": True})


@app.route("/api/stack/status", methods=["GET"])
def api_stack_status():
    with _stack_lock:
        return jsonify(dict(_stack_state))


@app.route("/api/stack/preview", methods=["GET"])
def api_stack_preview():
    with _stack_lock:
        png = _stack_preview_b64
    if not png:
        return jsonify({"error": "No stacked preview available yet"}), 404
    return Response(base64.b64decode(png), content_type="image/png")


@app.route("/api/pier-cam/snapshot")
def pier_cam_snapshot():
    with _pier_cam_frame_lock:
        frame = _pier_cam_frame
    if frame is None:
        return jsonify({"error": "No frame available"}), 404
    return Response(frame, content_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ── Object catalog ─────────────────────────────────────────────────────────────

_CATALOG_SKIP = frozenset([
    "Nonexistent object", "Duplicated record", "Object of other/unknown type",
])

def _build_dso_catalog() -> list[dict]:
    catalog: list[dict] = []
    for obj in _ongc_list():
        if obj.type in _CATALOG_SKIP or obj.coords is None:
            continue
        try:
            ra_h  = float(obj.coords[0][0] + obj.coords[0][1]/60 + obj.coords[0][2]/3600)
            d0    = obj.coords[1][0]
            sign  = -1 if d0 < 0 else 1
            dec_d = sign * float(abs(d0) + obj.coords[1][1]/60 + obj.coords[1][2]/3600)

            idents      = obj.identifiers
            messier_raw = idents[0]                      # "M042" or None
            names       = idents[3] or []

            if messier_raw:
                obj_id = "M" + str(int(messier_raw[1:]))
            elif obj.name.startswith("NGC"):
                obj_id = "NGC " + str(int(obj.name[3:]))
            elif obj.name.startswith("IC"):
                obj_id = "IC "  + str(int(obj.name[2:]))
            else:
                obj_id = obj.name

            catalog.append({
                "id":   obj_id,
                "name": names[0] if names else "",
                "type": obj.type,
                "ra":   round(ra_h,  4),
                "dec":  round(dec_d, 4),
            })
        except Exception:
            continue
    return catalog

_dso_catalog: list[dict] = _build_dso_catalog()
logger.info("DSO catalog built: %d objects", len(_dso_catalog))


@app.route("/api/catalog")
def api_catalog():
    return jsonify(_dso_catalog)


# ── Schedule execution ─────────────────────────────────────────────────────────

def _sched_cancelled() -> bool:
    with _sched_lock:
        return _sched_state["cancelled"]


def _sched_prepare_mount() -> None:
    """Best-effort unpark + tracking-on before a run, gated on safety."""
    if _tel is None:
        return
    if _safety_mgr is not None and not _safety_mgr.is_safe():
        logger.warning("Schedule: system unsafe at startup — not unparking")
        return
    try:
        with _device_lock:
            if _tel.is_parked():
                logger.info("Schedule: unparking mount")
                _tel.unpark()
            if not _tel.is_tracking():
                logger.info("Schedule: enabling tracking")
                _tel.set_tracking(True)
    except Exception as exc:
        logger.warning("Schedule: mount preparation failed: %s", exc)


def _run_schedule_observation(idx: int, item: dict) -> None:
    """Run a single scheduled observation. Exceptions here skip only this item."""
    target    = str(item.get("target", "Unknown"))
    ra        = float(item.get("ra", 0))      # decimal hours
    dec       = float(item.get("dec", 0))
    exp_dur   = float(item.get("expDur", 60))
    exp_count = int(item.get("expCount", 1))
    binning   = max(1, int(item.get("binning", 1)))
    start_str = item.get("startTime", "")

    with _sched_lock:
        _sched_state.update({
            "current_idx": idx,
            "current_target": target,
            "current_phase": "waiting",
            "current_frame": 0,
            "total_frames": exp_count,
        })

    # Wait until scheduled start time (max 2 h wait; skip if overdue)
    if start_str:
        try:
            sh, sm = map(int, start_str.split(":"))
            now = time.localtime()
            target_s = sh * 3600 + sm * 60
            now_s    = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
            wait_s   = target_s - now_s
            if wait_s < 0:
                wait_s += 86400
            if 0 < wait_s <= 7200:
                logger.info("Schedule: waiting %.0f s until %s for %s",
                            wait_s, start_str, target)
                deadline = time.monotonic() + wait_s
                while time.monotonic() < deadline and not _sched_cancelled():
                    time.sleep(1)
        except (ValueError, AttributeError) as exc:
            logger.debug("Schedule: start-time parse error: %s", exc)

    if _sched_cancelled():
        return

    # ── Slew ────────────────────────────────────────────────────────────────
    with _sched_lock:
        _sched_state["current_phase"] = "slewing"

    slew_ok = False
    if _tel is not None:
        rejection = _slew_rejection(ra, dec)
        if rejection is not None:
            logger.warning("Schedule: skipping %s — slew rejected: %s", target, rejection)
            with _sched_lock:
                _sched_state["error"] = f"{target}: {rejection}"
            return
        logger.info("Schedule: slewing to %s RA=%.4f h Dec=%.4f°", target, ra, dec)
        try:
            with _device_lock:
                _tel.begin_slew(ra, dec)
            slew_ok = _wait_slew_complete(timeout=180.0)
            if slew_ok:
                logger.info("Schedule: slew complete → %s", target)
            else:
                logger.error("Schedule: slew to %s timed out — skipping exposures", target)
                with _sched_lock:
                    _sched_state["error"] = f"Slew to {target} timed out"
        except Exception as exc:
            logger.error("Schedule: slew failed for %s: %s", target, exc)
            with _sched_lock:
                _sched_state["error"] = f"Slew to {target} failed: {exc}"
    else:
        logger.warning("Schedule: telescope not connected — skipping slew for %s", target)

    if _sched_cancelled():
        return

    # Don't expose if the slew didn't confirm a settled, on-target mount.
    if _tel is not None and not slew_ok:
        return

    # ── Expose ────────────────────────────────────────────────────────────────
    for frame in range(1, exp_count + 1):
        if _sched_cancelled():
            return
        with _sched_lock:
            _sched_state["current_phase"] = "exposing"
            _sched_state["current_frame"] = frame

        logger.info("Schedule: %s frame %d/%d (%.1fs bin%d)",
                    target, frame, exp_count, exp_dur, binning)

        if _cam is None:
            logger.warning("Schedule: camera not connected — skipping exposure for %s", target)
            return

        # Build FITS path when photometry is enabled so the pipeline has a
        # science-grade file to work with instead of a display PNG.
        fits_save_path: Optional[str] = None
        with _state_lock:
            phot_enabled = _state["photometry"]["enabled"]
        if phot_enabled:
            safe_tgt = "".join(c if c.isalnum() or c in "-_ " else "_" for c in target).strip()
            fits_save_path = str(
                pathlib.Path("data") / "fits" /
                f"{safe_tgt}_{frame:02d}_{int(time.time())}.fits"
            )

        try:
            _pier_cam_pause.set()
            _expose_cancel.clear()
            time.sleep(0.1)
            with _device_lock:
                _cam.set_binning(binning)
                _cam.expose(
                    duration=exp_dur, light=True,
                    cancel_check=lambda: _sched_cancelled() or _expose_cancel.is_set(),
                )
                b64 = _capture_image(fits_path=fits_save_path, exp_dur=exp_dur)
            if b64:
                _store_history_image(target, exp_dur, binning, frame, exp_count, b64)
            # Trigger photometry on the freshly saved FITS
            if fits_save_path and pathlib.Path(fits_save_path).exists():
                _enqueue_photometry(fits_save_path)
        except ExposureCancelled:
            logger.warning("Schedule: frame %d of %s aborted", frame, target)
            if _sched_cancelled():
                return
            # Single-frame abort — move on to the next frame.
        except Exception as exc:
            logger.error("Schedule: exposure failed %s frame %d: %s", target, frame, exc)
        finally:
            _pier_cam_pause.clear()


def _run_schedule_bg(items: list) -> None:
    """Background thread: slew + expose for each scheduled observation."""
    with _sched_lock:
        _sched_state.update({
            "running": True, "cancelled": False,
            "current_idx": -1, "current_target": "",
            "current_phase": "starting",
            "current_frame": 0, "total_frames": 0,
            "completed": 0, "total": len(items), "error": None,
        })
    logger.info("Schedule started: %d observations", len(items))

    _sched_prepare_mount()

    try:
        for idx, item in enumerate(items):
            if _sched_cancelled():
                break
            try:
                _run_schedule_observation(idx, item)
            except Exception as exc:
                # A single bad observation must not abort the whole night.
                logger.error("Schedule: observation %d (%s) failed: %s",
                             idx, item.get("target", "?"), exc)
                with _sched_lock:
                    _sched_state["error"] = str(exc)
            with _sched_lock:
                _sched_state["completed"] = idx + 1
            logger.info("Schedule: ✓ %s (%d/%d)",
                        item.get("target", "?"), idx + 1, len(items))
    except Exception as exc:
        logger.error("Schedule crashed: %s", exc)
        with _sched_lock:
            _sched_state["error"] = str(exc)
    finally:
        with _sched_lock:
            _sched_state["running"] = False
            _sched_state["current_phase"] = (
                "cancelled" if _sched_state["cancelled"] else "done"
            )
        logger.info("Schedule finished")


@app.route("/api/schedule/run", methods=["POST"])
def api_schedule_run():
    with _sched_lock:
        if _sched_state["running"]:
            return jsonify({"error": "Schedule already running"}), 409
    data  = request.get_json(force=True) or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No items provided"}), 400

    valid, err = _validate_schedule_items(items)
    if err is not None:
        return jsonify({"error": err}), 400

    threading.Thread(
        target=_run_schedule_bg, args=(valid,),
        daemon=True, name="sched-runner",
    ).start()
    logger.info("Schedule run requested: %d items", len(valid))
    return jsonify({"ok": True})


def _validate_schedule_items(items: list) -> tuple[list, Optional[str]]:
    """Validate + normalize schedule items at the API boundary.

    Returns (normalized_items, None) on success, or ([], error_message) on the
    first invalid item.  Mirrors the bounds enforced by /api/slew so a buggy or
    crafted client can't drive the mount to garbage coordinates.
    """
    if not isinstance(items, list):
        return [], "items must be a list"
    out: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return [], f"item {i + 1} is not an object"
        label = item.get("target", f"#{i + 1}")
        try:
            ra        = float(item.get("ra", 0))
            dec       = float(item.get("dec", 0))
            exp_dur   = float(item.get("expDur", 60))
            exp_count = int(item.get("expCount", 1))
            binning   = int(item.get("binning", 1))
        except (TypeError, ValueError):
            return [], f"item '{label}' has non-numeric ra/dec/exposure fields"
        if not (0.0 <= ra < 24.0):
            return [], f"item '{label}': RA must be in [0, 24) hours"
        if not (-90.0 <= dec <= 90.0):
            return [], f"item '{label}': Dec must be in [-90, 90]°"
        if exp_dur <= 0:
            return [], f"item '{label}': exposure duration must be > 0"
        if exp_count < 1:
            return [], f"item '{label}': exposure count must be ≥ 1"
        if binning < 1:
            return [], f"item '{label}': binning must be ≥ 1"
        out.append({
            "target": str(item.get("target", "Unknown")),
            "ra": ra, "dec": dec, "expDur": exp_dur,
            "expCount": exp_count, "binning": binning,
            "startTime": str(item.get("startTime", "")),
        })
    return out, None


@app.route("/api/schedule/status", methods=["GET"])
def api_schedule_status():
    with _sched_lock:
        return jsonify(dict(_sched_state))


@app.route("/api/schedule/abort", methods=["DELETE"])
def api_schedule_abort():
    with _sched_lock:
        if _sched_state["running"]:
            _sched_state["cancelled"] = True
    logger.info("Schedule abort requested")
    return jsonify({"ok": True})


@app.route("/api/history", methods=["GET"])
def api_history():
    with _img_history_lock:
        images = list(reversed(_img_history))
    return jsonify({"images": images})


@app.route("/api/history/<img_id>", methods=["GET"])
def api_history_image(img_id: str):
    with _img_full_lock:
        b64 = _img_full.get(img_id)
    if not b64:
        # Lazy-load from disk if not in memory cache
        disk_path = _IMAGES_DIR / f"{img_id}.png"
        if disk_path.exists():
            with open(disk_path, "rb") as _f:
                b64 = base64.b64encode(_f.read()).decode()
            with _img_full_lock:
                _img_full[img_id] = b64
    if not b64:
        return jsonify({"error": "Image not found"}), 404
    return Response(
        base64.b64decode(b64), content_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/api/history/<img_id>/metadata", methods=["PATCH"])
def api_history_patch_metadata(img_id: str):
    data = request.get_json(silent=True) or {}
    allowed = {"target", "exp_dur", "binning", "frame", "total"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "No valid fields provided"}), 400
    with _img_history_lock:
        for entry in _img_history:
            if entry["id"] == img_id:
                for k, v in updates.items():
                    entry[k] = v
                _save_history_to_disk()
                return jsonify({"ok": True, "entry": entry})
    return jsonify({"error": "Image not found"}), 404


@app.route("/api/geocode", methods=["GET"])
def api_geocode():
    """Resolve a place name to lat/lon using OpenStreetMap Nominatim."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q parameter required"}), 400
    try:
        import requests as _req
        resp = _req.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1},
            headers={"User-Agent": "TheTelescopeNode/1.0"},
            timeout=8,
        )
        results = resp.json()
        if not results:
            return jsonify({"error": f"No results found for '{q}'"}), 404
        r = results[0]
        return jsonify({
            "latitude":    round(float(r["lat"]), 6),
            "longitude":   round(float(r["lon"]), 6),
            "display_name": r.get("display_name", q),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── HTML template ──────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NODE v1 — ALPACA Control</title>
<style>
:root {
  --bg:        #000000;
  --surface:   rgba(14, 16, 22, 0.60);
  --surface2:  rgba(20, 22, 32, 0.72);
  --border:    rgba(255, 255, 255, 0.08);
  --green:     #00e676;
  --green-hi:  #69ff9c;
  --yellow:    #ffd740;
  --red:       #ff5252;
  --blue:      #448aff;
  --gray:      #4a5060;
  --text:      #e2e8f0;
  --dim:       #8892a4;
  --mono:      'Courier New', 'Consolas', monospace;
  --glass-blur: blur(28px) saturate(160%);
  --glass-border: 1px solid rgba(255,255,255,0.09);
  --glass-shine: inset 0 1px 0 rgba(255,255,255,0.06);
  --glass-shadow: 0 8px 32px rgba(0,0,0,0.7);
  --panel-bg:  rgba(14, 16, 22, 0.60);
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
  position: relative;
}
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background:
    radial-gradient(ellipse 90% 55% at 50% 35%, rgba(18,28,90,0.38) 0%, transparent 70%),
    radial-gradient(ellipse 50% 40% at 80% 80%, rgba(10,35,60,0.25) 0%, transparent 65%);
  pointer-events: none;
  z-index: 0;
}
body > * { position: relative; z-index: 1; }

/* ── Header ── */
.hdr {
  background: rgba(0,0,0,0.72);
  backdrop-filter: blur(20px) saturate(140%);
  -webkit-backdrop-filter: blur(20px) saturate(140%);
  border-bottom: var(--glass-border);
  padding: 10px 20px;
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
  box-shadow: 0 1px 0 rgba(255,255,255,0.04), 0 4px 16px rgba(0,0,0,0.5);
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
  border-radius: 8px;
  color: var(--green);
  user-select: none;
  background: rgba(0,230,118,0.05);
  box-shadow: 0 0 10px rgba(0,230,118,0.12);
}
.conn-pill.clickable:hover {
  background: rgba(0,230,118,0.12);
  box-shadow: 0 0 16px rgba(0,230,118,0.22);
}
.conn-dropdown {
  display: none;
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  background: rgba(10,12,20,0.92);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: var(--glass-border);
  border-radius: 10px;
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
  background: rgba(255,255,255,0.04);
  font-family: var(--mono);
  font-size: 11px;
  cursor: pointer;
  letter-spacing: 1px;
  text-transform: uppercase;
  border-radius: 8px;
  transition: background 0.15s, color 0.15s, box-shadow 0.15s, transform 0.1s;
}
.btn:hover:not(:disabled) { transform: translateY(-1px); }
.btn-green  { border-color: var(--green);  color: var(--green); }
.btn-green:hover:not(:disabled)  { background: var(--green);  color: #000; box-shadow: 0 0 14px rgba(0,230,118,0.45); }
.btn-red    { border-color: var(--red);    color: var(--red); }
.btn-red:hover:not(:disabled)    { background: var(--red);    color: #000; box-shadow: 0 0 14px rgba(255,82,82,0.45); }
.btn-blue   { border-color: var(--blue);   color: var(--blue); }
.btn-blue:hover:not(:disabled)   { background: var(--blue);   color: #000; box-shadow: 0 0 14px rgba(68,138,255,0.45); }
.btn-yellow { border-color: var(--yellow); color: var(--yellow); }
.btn-yellow:hover:not(:disabled) { background: var(--yellow); color: #000; box-shadow: 0 0 14px rgba(255,215,64,0.45); }
.btn-dim    { border-color: var(--gray);   color: var(--dim); }
.btn-dim:hover:not(:disabled)    { background: var(--gray);   color: var(--text); }
.btn:disabled { opacity: 0.3; cursor: not-allowed; }
.btn-full { width: 100%; }

/* ── Dot indicators ── */
.dot {
  width: 8px; height: 8px; border-radius: 50%;
  display: inline-block; flex-shrink: 0;
}
.dot-green  { background: var(--green);  box-shadow: 0 0 8px var(--green), 0 0 16px rgba(0,230,118,0.3); }
.dot-yellow { background: var(--yellow); box-shadow: 0 0 8px var(--yellow), 0 0 16px rgba(255,215,64,0.3); }
.dot-red    { background: var(--red);    box-shadow: 0 0 8px var(--red), 0 0 16px rgba(255,82,82,0.3); }
.dot-gray   { background: var(--gray); }

@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.35; } }
.pulse { animation: pulse 1.1s ease-in-out infinite; }

/* ── Layout ── */
.main {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
  padding: 10px;
  background: transparent;
  overflow: hidden;
  min-height: 0;
}

/* ── Resize handles ── */
.resize-handle-h {
  flex-shrink: 0;
  height: 8px;
  cursor: ns-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  z-index: 10;
}
.resize-handle-h::after {
  content: '';
  width: 48px; height: 3px;
  border-radius: 2px;
  background: rgba(255,255,255,0.1);
  transition: background 0.2s, width 0.2s;
}
.resize-handle-h:hover::after,
.resize-handle-h.dragging::after {
  background: rgba(255,255,255,0.38);
  width: 72px;
}
.resize-handle-v {
  flex-shrink: 0;
  width: 8px;
  cursor: ew-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  z-index: 10;
}
.resize-handle-v::after {
  content: '';
  width: 3px; height: 48px;
  border-radius: 2px;
  background: rgba(255,255,255,0.1);
  transition: background 0.2s, height 0.2s;
}
.resize-handle-v:hover::after,
.resize-handle-v.dragging::after {
  background: rgba(255,255,255,0.38);
  height: 72px;
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
  height: 180px; /* overridden by JS resizer */
  min-height: 40px;
  background: rgba(0,0,0,0.6);
  backdrop-filter: blur(20px) saturate(140%);
  -webkit-backdrop-filter: blur(20px) saturate(140%);
  border-top: var(--glass-border);
  box-shadow: var(--glass-shine);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Panels ── */
.panel {
  background: var(--surface);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: var(--glass-border);
  border-radius: 14px;
  box-shadow: var(--glass-shine), var(--glass-shadow);
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
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: var(--glass-border);
  border-radius: 14px;
  box-shadow: var(--glass-shine), var(--glass-shadow);
  display: flex; flex-direction: row;
  overflow: hidden; min-height: 0;
}
.img-col.hidden { display: none; }

.img-sub {
  background: var(--surface);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: var(--glass-border);
  border-radius: 14px;
  box-shadow: var(--glass-shine), var(--glass-shadow);
  padding: 14px 20px;
  display: flex; flex-direction: column; gap: 10px;
  overflow: hidden; min-height: 0;
}
.img-sub.hidden { display: none; }
.img-sub::-webkit-scrollbar { width: 6px; }
.img-sub::-webkit-scrollbar-track { background: var(--bg); }
.img-sub::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.img-sub::-webkit-scrollbar-thumb:hover { background: var(--gray); }

/* ── Embedded panels (camera + telescope in main grid) ── */
.panel-embed {
  background: var(--surface);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: var(--glass-border);
  border-radius: 14px;
  box-shadow: var(--glass-shine), var(--glass-shadow);
  display: flex; flex-direction: column;
  overflow: hidden; min-height: 0;
}
.panel-embed.hidden { display: none; }
.panel-embed-inner {
  flex: 1; overflow-y: auto;
  padding: 20px;
  display: flex; flex-direction: column; gap: 16px;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.1) transparent;
}
.panel-embed-inner::-webkit-scrollbar { width: 5px; }
.panel-embed-inner::-webkit-scrollbar-track { background: transparent; }
.panel-embed-inner::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }

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
  padding: 2px 8px; font-size: 10px; letter-spacing: 1px;
  text-transform: uppercase; border: 1px solid var(--gray); color: var(--gray);
  border-radius: 20px; background: rgba(255,255,255,0.03);
}
.badge-on   { border-color: var(--green);  color: var(--green);  background: rgba(0,230,118,0.07);  box-shadow: 0 0 8px rgba(0,230,118,0.2); }
.badge-warn { border-color: var(--yellow); color: var(--yellow); background: rgba(255,215,64,0.07); }
.badge-err  { border-color: var(--red);    color: var(--red);    background: rgba(255,82,82,0.07); }

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
  background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1);
  color: var(--text); font-family: var(--mono); font-size: 13px;
  padding: 6px 10px; width: 100%; border-radius: 8px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.inp:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 2px rgba(68,138,255,0.2); }
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
  background: rgba(0,0,0,0.7);
  backdrop-filter: blur(12px) saturate(120%);
  -webkit-backdrop-filter: blur(12px) saturate(120%);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
}
.modal.hidden { display: none; }

.modal-content {
  background: rgba(12,14,22,0.82);
  backdrop-filter: blur(40px) saturate(180%);
  -webkit-backdrop-filter: blur(40px) saturate(180%);
  border: var(--glass-border);
  border-radius: 18px;
  box-shadow: var(--glass-shine), 0 24px 80px rgba(0,0,0,0.85);
  padding: 28px;
  max-height: 90vh;
  overflow-y: auto;
  overflow-x: visible;
  width: 90%;
  max-width: 600px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.1) transparent;
}
.modal-content::-webkit-scrollbar { width: 5px; }
.modal-content::-webkit-scrollbar-track { background: transparent; }
.modal-content::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }

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
  background: rgba(0,0,0,0.75);
  backdrop-filter: blur(16px) saturate(120%);
  -webkit-backdrop-filter: blur(16px) saturate(120%);
  display: flex; align-items: center; justify-content: center;
  z-index: 50;
}
.overlay.hidden { display: none; }
.card {
  background: rgba(10,12,22,0.85);
  backdrop-filter: blur(40px) saturate(170%);
  -webkit-backdrop-filter: blur(40px) saturate(170%);
  border: var(--glass-border);
  border-radius: 18px;
  box-shadow: var(--glass-shine), 0 24px 80px rgba(0,0,0,0.85);
  padding: 24px 28px; width: 420px;
  display: flex; flex-direction: column; gap: 14px;
  max-height: 90vh; overflow: hidden;
}
.card-title { font-size: 13px; letter-spacing: 2px; text-transform: uppercase; color: var(--green-hi); }
.inp-row { display: flex; gap: 8px; }
.srv-list { display: flex; flex-direction: column; gap: 5px; max-height: 320px; overflow-y: auto; scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.1) transparent; }
.srv-list::-webkit-scrollbar { width: 5px; }
.srv-list::-webkit-scrollbar-track { background: transparent; }
.srv-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
.srv-item {
  padding: 7px 12px; border: var(--glass-border);
  border-radius: 8px; background: rgba(255,255,255,0.03);
  cursor: pointer; color: var(--blue); transition: border-color .15s, background .15s, box-shadow .15s;
}
.srv-item:hover { border-color: rgba(68,138,255,0.4); background: rgba(68,138,255,.08); box-shadow: 0 0 12px rgba(68,138,255,0.15); }
.srv-item.selected { border-color: rgba(68,138,255,0.7); background: rgba(68,138,255,.14); }
.sep { border-top: var(--glass-border); }

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

/* ── Schedule Modal ── */
.sched-modal .modal-content {
  max-width: 1020px; width: 96%; max-height: 92vh; height: 92vh;
  gap: 0; padding: 0; overflow: hidden;
}
.sched-hdr {
  padding: 18px 24px 14px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 12px; flex-shrink: 0;
}
.sched-title { font-size: 16px; font-weight: bold; letter-spacing: 1px; flex: 1; }
.sched-night-inp {
  background: var(--bg); border: 1px solid var(--border);
  color: var(--text); font-family: var(--mono); font-size: 12px;
  padding: 3px 8px; width: 74px; text-align: center;
}
.sched-night-inp:focus { outline: none; border-color: var(--blue); }

/* Timeline */
.sched-tl-wrap {
  padding: 12px 24px 10px; border-bottom: 1px solid var(--border);
  flex-shrink: 0; background: var(--bg);
}
.sched-tl-labels {
  position: relative; height: 14px; margin-bottom: 4px;
  font-size: 9px; color: var(--dim); letter-spacing: 1px;
}
.sched-tl-track {
  position: relative; height: 34px;
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--border); border-radius: 4px; overflow: hidden;
}
.sched-tl-block {
  position: absolute; top: 3px; bottom: 3px; border-radius: 3px;
  display: flex; align-items: center; padding: 0 6px;
  font-size: 9px; letter-spacing: 0.5px; overflow: hidden;
  white-space: nowrap; cursor: pointer; transition: filter 0.15s; min-width: 4px;
}
.sched-tl-block:hover { filter: brightness(1.25); }
.sched-tl-block.conflict { outline: 2px solid var(--red); }
.sched-tl-now {
  position: absolute; top: 0; bottom: 0; width: 2px;
  background: var(--yellow); opacity: 0.75; pointer-events: none;
}

/* Body */
.sched-body {
  flex: 1; overflow-y: auto; padding: 14px 24px;
  display: flex; flex-direction: column; gap: 8px;
  min-height: 0; position: relative;
}
.sched-body::-webkit-scrollbar { width: 6px; }
.sched-body::-webkit-scrollbar-track { background: var(--bg); }
.sched-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.sched-empty {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  flex: 1; gap: 10px; color: var(--gray); font-size: 12px;
  letter-spacing: 2px; padding: 40px; text-align: center;
}

/* Schedule items */
.sched-item {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 11px 14px; border: var(--glass-border);
  background: rgba(255,255,255,0.03); border-radius: 10px;
  box-shadow: var(--glass-shine);
  transition: border-color 0.15s, opacity 0.12s, box-shadow 0.15s; cursor: default;
}
.sched-item:hover { border-color: rgba(255,255,255,0.18); box-shadow: var(--glass-shine), 0 4px 16px rgba(0,0,0,0.4); }
.sched-item.conflict { border-color: var(--red); }
.sched-item.dragging { opacity: 0.4; }
.sched-item.drag-over { border-color: var(--blue); background: rgba(88,166,255,0.05); }

.sched-drag {
  color: var(--gray); cursor: grab; font-size: 16px;
  user-select: none; flex-shrink: 0; line-height: 1; margin-top: 3px;
}
.sched-drag:active { cursor: grabbing; }
.sched-item-num {
  font-size: 10px; color: var(--dim); width: 18px;
  text-align: right; flex-shrink: 0; padding-top: 3px;
}
.sched-item-main { flex: 1; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.sched-item-name {
  font-size: 13px; color: var(--text); font-weight: bold;
  display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
}
.sched-type-badge {
  font-size: 9px; padding: 1px 5px; border: 1px solid var(--gray);
  color: var(--gray); letter-spacing: 1px; text-transform: uppercase;
  border-radius: 2px; flex-shrink: 0;
}
.sched-item-coords { font-size: 11px; color: var(--dim); }
.sched-item-exp { font-size: 11px; color: var(--blue); display: flex; align-items: center; gap: 8px; }
.sched-item-note { font-size: 10px; color: var(--dim); font-style: italic; }
.sched-item-time {
  display: flex; flex-direction: column; align-items: flex-end;
  gap: 4px; flex-shrink: 0; min-width: 100px;
}
.sched-time-inp {
  background: var(--bg); border: 1px solid var(--border);
  color: var(--green-hi); font-family: var(--mono); font-size: 14px;
  padding: 3px 8px; width: 76px; text-align: center; font-weight: bold;
}
.sched-time-inp:focus { outline: none; border-color: var(--green); }
.sched-dur { font-size: 10px; color: var(--dim); letter-spacing: 1px; }
.sched-dur span { color: var(--text); }
.sched-item-btns { display: flex; gap: 4px; flex-shrink: 0; align-items: flex-start; }

/* Footer */
.sched-footer {
  padding: 11px 24px; border-top: 1px solid var(--border);
  display: flex; align-items: center; gap: 10px; flex-shrink: 0;
  background: var(--surface);
}
.sched-stats { font-size: 10px; color: var(--dim); letter-spacing: 1px; flex: 1; }
.sched-stats span { color: var(--text); }

/* Sky conditions bar */
.sched-sky-bar {
  display: none; padding: 7px 24px;
  border-bottom: 1px solid var(--border);
  background: rgba(255,255,255,0.015);
  flex-shrink: 0; align-items: center; gap: 20px; flex-wrap: wrap;
  font-size: 10px; letter-spacing: 1px; color: var(--dim);
}
.sched-sky-bar strong { color: var(--text); }
.sched-sky-attr { display: flex; align-items: center; gap: 5px; }

/* Add / Edit window — fixed viewport overlay, above modal stack */
.sched-add-window {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.78);
  backdrop-filter: blur(16px) saturate(130%);
  -webkit-backdrop-filter: blur(16px) saturate(130%);
  display: flex; align-items: center; justify-content: center;
  z-index: 210; padding: 20px;
}
.sched-add-card {
  background: rgba(10,12,22,0.88);
  backdrop-filter: blur(40px) saturate(170%);
  -webkit-backdrop-filter: blur(40px) saturate(170%);
  border: var(--glass-border);
  border-radius: 18px; width: 100%; max-width: 580px;
  max-height: 90vh; overflow-y: auto;
  display: flex; flex-direction: column;
  box-shadow: var(--glass-shine), 0 24px 80px rgba(0,0,0,0.9);
}
.sched-add-card::-webkit-scrollbar { width: 5px; }
.sched-add-card::-webkit-scrollbar-track { background: transparent; }
.sched-add-card::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
.sched-add-hdr {
  padding: 16px 22px 14px; border-bottom: var(--glass-border);
  display: flex; align-items: center; justify-content: space-between;
  font-size: 13px; font-weight: bold; letter-spacing: 1px; flex-shrink: 0;
  background: rgba(255,255,255,0.03); border-radius: 18px 18px 0 0;
  position: sticky; top: 0; z-index: 1;
}
.sched-add-section {
  padding: 16px 22px; border-bottom: var(--glass-border);
  display: flex; flex-direction: column; gap: 10px;
}
.sched-add-section-lbl {
  font-size: 9px; letter-spacing: 2px; text-transform: uppercase;
  color: var(--dim); margin-bottom: 2px;
}
.sched-add-foot {
  padding: 14px 22px; border-top: var(--glass-border);
  display: flex; gap: 8px; justify-content: flex-end; flex-shrink: 0;
  background: rgba(255,255,255,0.02); border-radius: 0 0 18px 18px;
  position: sticky; bottom: 0; z-index: 1;
}
.sched-hint {
  font-size: 10px; color: var(--dim); padding: 8px 12px;
  border: 1px solid var(--border); border-left: 3px solid var(--blue);
  background: rgba(88,166,255,0.04); line-height: 1.75; border-radius: 0 3px 3px 0;
}
.sched-hint strong { color: var(--text); }
.sched-alt-pill {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 10px; padding: 3px 10px; border-radius: 3px;
  border: 1px solid var(--border); color: var(--dim); letter-spacing: 0.5px;
}
.sched-alt-pill.good   { border-color: var(--green);  color: var(--green);  background: rgba(63,185,80,0.07); }
.sched-alt-pill.ok     { border-color: var(--yellow); color: var(--yellow); background: rgba(210,153,34,0.07); }
.sched-alt-pill.bad    { border-color: var(--red);    color: var(--red);    background: rgba(248,81,73,0.07); }
.sched-alt-pill.none   { border-color: var(--gray);   color: var(--gray); }

/* Timeline block colours */
.sc0 { background: rgba(63,185,80,0.75);   color: #fff; }
.sc1 { background: rgba(88,166,255,0.75);  color: #fff; }
.sc2 { background: rgba(210,153,34,0.75);  color: #fff; }
.sc3 { background: rgba(188,129,255,0.75); color: #fff; }
.sc4 { background: rgba(255,127,80,0.75);  color: #fff; }
.sc5 { background: rgba(64,220,220,0.75);  color: #333; }
.sc6 { background: rgba(200,200,80,0.75);  color: #333; }
.sc7 { background: rgba(255,150,200,0.75); color: #333; }

/* ── Schedule live status bar ── */
.sched-live {
  flex-shrink: 0; padding: 10px 24px;
  background: rgba(63,185,80,0.07);
  border-bottom: 1px solid var(--green);
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}
.sched-live-pulse { width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);flex-shrink:0; }
.sched-live-target { font-size:13px;font-weight:bold;color:var(--green-hi);flex:1; }
.sched-live-phase {
  font-size:10px;letter-spacing:1px;color:var(--dim);text-transform:uppercase;
}
.sched-live-bar-wrap {
  width:100%; height:4px; background:rgba(255,255,255,0.06);
  border-radius:2px; overflow:hidden; margin-top:2px;
}
.sched-live-bar {
  height:100%; background:var(--green);
  border-radius:2px; transition:width 0.4s ease;
}
.sched-item.done-item { opacity: 0.45; }
.sched-item.done-item .sched-item-name::before {
  content: "✓ "; color: var(--green);
}
.sched-item.active-item { border-color: var(--green); background: rgba(63,185,80,0.06); }

/* ── Camera History Modal ── */
.hist-modal .modal-content {
  max-width: 1060px; width: 96%; max-height: 92vh; height: 92vh;
  gap: 0; padding: 0; overflow: hidden;
}
.hist-hdr {
  padding: 16px 24px 14px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 12px; flex-shrink: 0;
}
.hist-title { font-size:16px;font-weight:bold;letter-spacing:1px;flex:1; }
.hist-search {
  background:var(--bg);border:1px solid var(--border);
  color:var(--text);font-family:var(--mono);font-size:12px;
  padding:5px 12px;width:220px;
}
.hist-search:focus { outline:none;border-color:var(--blue); }
.hist-body {
  flex:1;overflow-y:auto;padding:16px 20px;
  display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
  gap:14px;align-content:start;min-height:0;
}
.hist-body::-webkit-scrollbar { width:6px; }
.hist-body::-webkit-scrollbar-track { background:var(--bg); }
.hist-body::-webkit-scrollbar-thumb { background:var(--border);border-radius:3px; }
.hist-empty {
  grid-column:1/-1;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:10px;
  color:var(--gray);font-size:12px;letter-spacing:2px;padding:60px;text-align:center;
}
.hist-card {
  background:rgba(255,255,255,0.03);border:var(--glass-border);
  border-radius:12px;overflow:hidden;cursor:pointer;
  box-shadow:var(--glass-shine);
  transition:border-color 0.15s,transform 0.15s,box-shadow 0.15s;display:flex;flex-direction:column;
}
.hist-card:hover { border-color:rgba(68,138,255,0.4);transform:translateY(-3px);box-shadow:var(--glass-shine),0 8px 24px rgba(68,138,255,0.2); }
.hist-thumb {
  width:100%;aspect-ratio:1;background:#000;
  display:flex;align-items:center;justify-content:center;overflow:hidden;
}
.hist-thumb img { width:100%;height:100%;object-fit:cover;image-rendering:pixelated; }
.hist-card-body { padding:8px 10px;display:flex;flex-direction:column;gap:3px; }
.hist-card-name { font-size:11px;font-weight:bold;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis; }
.hist-card-meta { font-size:9px;color:var(--dim);letter-spacing:0.5px; }
.hist-card-exp  { font-size:9px;color:var(--blue); }
.hist-footer {
  padding:10px 24px;border-top:1px solid var(--border);
  display:flex;align-items:center;gap:10px;flex-shrink:0;background:var(--surface);
}
.hist-count { font-size:10px;color:var(--dim);letter-spacing:1px;flex:1; }
.hist-count span { color:var(--text); }

/* Lightbox */
.hist-lightbox {
  position:fixed;inset:0;background:rgba(0,0,0,0.92);
  backdrop-filter:blur(8px);
  display:flex;align-items:center;justify-content:center;
  z-index:300;flex-direction:column;gap:14px;padding:24px;
}
.hist-lightbox.hidden { display:none; }
.hist-lb-img {
  max-width:90vw;max-height:78vh;object-fit:contain;
  image-rendering:pixelated;border:1px solid var(--border);
}
.hist-lb-meta {
  font-size:12px;color:var(--dim);letter-spacing:1px;text-align:center;line-height:2;
}
.hist-lb-meta strong { color:var(--text); }
.hist-lb-btns { display:flex;gap:10px; }
.hist-lb-edit {
  background:var(--surface2);border:var(--glass-border);border-radius:10px;
  padding:14px 18px;display:flex;flex-direction:column;gap:8px;min-width:260px;
}
.hist-lb-edit.hidden { display:none; }
.hist-lb-edit-row { display:flex;align-items:center;gap:10px; }
.hist-lb-edit-row label {
  font-size:10px;letter-spacing:1px;color:var(--dim);text-transform:uppercase;width:90px;flex-shrink:0;
}
.hist-lb-edit-inp {
  flex:1;background:var(--surface);border:1px solid var(--border);border-radius:5px;
  color:var(--text);font-size:12px;padding:4px 8px;font-family:inherit;
}
.hist-lb-edit-inp:focus { outline:none;border-color:var(--green); }
.hist-lb-edit-actions { display:flex;gap:8px;justify-content:flex-end;margin-top:4px; }

/* ── Night-vision mode (red monochrome) ── */
html[data-night] {
  --bg:        #050000;
  --surface:   rgba(30, 4, 4, 0.65);
  --surface2:  rgba(40, 6, 6, 0.75);
  --border:    rgba(200, 0, 0, 0.15);
  --glass-border: 1px solid rgba(200,0,0,0.12);
  --green:     #cc1100;
  --green-hi:  #ff3300;
  --yellow:    #991100;
  --red:       #ff2200;
  --blue:      #aa1100;
  --gray:      #4a2020;
  --text:      #ff8080;
  --dim:       #882020;
}
html[data-night] body::before {
  background: radial-gradient(ellipse 90% 55% at 50% 35%, rgba(80,8,8,0.35) 0%, transparent 70%);
}
html[data-night] .hdr-logo { color: var(--green-hi); }
html[data-night] img, html[data-night] video { filter: none; }

/* ── Alt sparkline canvas ── */
.sched-sparkline {
  flex-shrink: 0; border-radius: 4px; display: block;
  border: 1px solid rgba(255,255,255,0.06);
  background: rgba(0,0,0,0.3);
}

/* ── Exposure ring ── */
.exp-ring-wrap {
  display: flex; align-items: center; gap: 14px;
}
.exp-ring {
  flex-shrink: 0; display: none;
}
.exp-ring.active { display: block; }
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
    <button id="safetyResetBtn" onclick="resetSafety()" title="Clear the latched unsafe state and resume operations"
            style="display:none;margin-left:6px;font-size:10px;padding:1px 6px;cursor:pointer;">Reset</button>
  </div>

  <div style="color:var(--dim);font-size:11px;" id="sunEl"></div>

  <div id="errBanner" style="display:none;color:var(--red);font-size:11px;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title=""></div>
  <div class="hdr-right">
    <button class="btn btn-dim" id="btnNightMode" onclick="toggleNightMode()" title="Toggle night-vision mode" style="font-size:13px;padding:4px 10px;letter-spacing:0;">👁</button>
    <button class="btn btn-dim" id="btnConfig" onclick="openConfigModal()">Config</button>
    <button class="btn btn-dim" id="btnSchedule" onclick="openScheduleModal()" style="border-color:var(--blue);color:var(--blue);display:none;">🗓 Schedule</button>
    <button class="btn btn-dim" id="btnHistory"  onclick="openHistoryModal()" style="border-color:var(--yellow);color:var(--yellow);position:relative;">
      📷 History<span id="histBadge" style="display:none;position:absolute;top:-5px;right:-5px;background:var(--yellow);color:#000;border-radius:50%;width:16px;height:16px;font-size:9px;display:none;align-items:center;justify-content:center;font-weight:bold;"></span>
    </button>
  </div>
</div>

<!-- Setup banner — visible until activation code is entered or node is registered -->
<div id="setupBanner" style="display:none;flex-direction:row;background:linear-gradient(90deg,rgba(160,185,255,0.12),rgba(160,185,255,0.06));
     border-bottom:1px solid rgba(160,185,255,0.2);padding:10px 20px;
     align-items:center;gap:12px;font-size:13px;">
  <span style="color:rgba(160,185,255,0.9);">&#9679;</span>
  <span style="color:#c8d8ff;flex:1;">
    <strong style="color:#fff;">Not connected to The Telescope Net.</strong>
    &nbsp;Get your activation code from <a href="https://app.thetelescope.net" target="_blank" style="color:#a0b9ff;">app.thetelescope.net</a>
    → Connect telescope, then paste it here.
  </span>
  <button onclick="openWelcomeModal()"
    style="background:#a0b9ff;color:#000814;border:none;border-radius:6px;
           padding:6px 14px;font-size:12px;font-weight:700;cursor:pointer;
           letter-spacing:0.3px;white-space:nowrap;">
    Enter code
  </button>
</div>

<!-- Main grid -->
<div class="main" id="mainGrid">

  <!-- Empty state shown when nothing to display -->
  <div class="main-empty" id="mainEmpty" style="grid-column:1/-1;background:transparent;display:none;">
    <div style="font-size:32px;opacity:0.25">✦</div>
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

  <!-- Panel row: telescope + camera side by side with draggable divider -->
  <div id="panelRow" style="display:flex;min-height:0;overflow:hidden;gap:0;">
  <!-- Telescope Panel -->
  <div class="panel-embed" id="telModal" style="flex:1;min-width:0;">
    <div class="panel-embed-inner">
      <div class="modal-header">
        <div class="modal-title">
          <span class="dot dot-gray" id="telModalDot"></span>
          🔭 Telescope Control
          <div class="badges" id="telModalBadges" style="margin-left:8px;"></div>
        </div>
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

      <!-- Arm controls (shown only when CoverCalibrator is available) -->
      <div class="ctrl-group" id="armCtrlGroup" style="display:none;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
          <div class="panel-label" style="margin:0;">Arm</div>
          <span id="armStateLabel" style="font-size:11px;color:var(--dim);letter-spacing:1px;"></span>
        </div>
        <div class="ctrl-row">
          <button class="btn btn-green" id="btnArmOpen"  onclick="apiArmOpen()"  disabled>Open Arm</button>
          <button class="btn btn-dim"   id="btnArmClose" onclick="apiArmClose()" disabled>Close Arm</button>
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
          <div id="catalogDropdown" style="display:none;position:fixed;z-index:9999;
            background:#161b22;border:1px solid var(--border);border-radius:6px;
            max-height:200px;overflow-y:auto;box-shadow:0 4px 16px rgba(0,0,0,0.7);"></div>
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
        <div id="slewRejectionBanner" style="display:none;margin-top:8px;padding:8px 10px;background:rgba(220,50,50,0.15);border:1px solid #c0392b;border-radius:6px;font-size:12px;color:#e74c3c;">
          <div id="slewRejectionMsg" style="margin-bottom:6px;"></div>
          <button class="btn btn-full" id="btnForceSlew" onclick="apiForceSlew()"
            style="background:#c0392b;color:#fff;border:none;padding:5px 0;border-radius:4px;cursor:pointer;font-size:12px;letter-spacing:0.5px;">
            ⚠ Force Slew — I accept the risks
          </button>
        </div>
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

  <div class="resize-handle-v" id="panelResizer"></div>
  <!-- Camera Panel -->
  <div class="panel-embed" id="camModal" style="flex:1;min-width:0;">
    <div class="panel-embed-inner">
      <div class="modal-header">
        <div class="modal-title">
          <span class="dot dot-gray" id="camModalDot"></span>
          📷 Camera Control
        </div>
      </div>

      <!-- State display -->
      <div style="border-bottom: 1px solid var(--border); padding-bottom: 12px;">
        <div class="exp-ring-wrap">
          <div>
            <div class="cam-state cs-idle" id="camModalState">—</div>
            <div class="cam-sub" id="camModalSub"></div>
            <div id="camModalReady" style="font-size:11px;color:var(--gray)"></div>
          </div>
          <svg id="expRing" class="exp-ring" width="72" height="72" viewBox="0 0 72 72">
            <circle cx="36" cy="36" r="30" fill="none" stroke="rgba(255,255,255,0.07)" stroke-width="5"/>
            <circle id="expRingArc" cx="36" cy="36" r="30" fill="none"
              stroke="var(--yellow)" stroke-width="5" stroke-linecap="round"
              stroke-dasharray="188.5" stroke-dashoffset="0"
              transform="rotate(-90 36 36)" style="transition:stroke 0.3s;"/>
            <text id="expRingText" x="36" y="40" text-anchor="middle"
              fill="var(--text)" font-size="11" font-family="monospace" letter-spacing="0">—</text>
          </svg>
        </div>
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

  </div><!-- /panelRow -->

</div><!-- /main -->

<!-- Camera History Modal -->
<div class="modal hidden hist-modal" id="histModal" onclick="if(event.target===this)closeHistoryModal()">
  <div class="modal-content">
    <div class="hist-hdr">
      <span style="font-size:22px;flex-shrink:0;">📷</span>
      <div class="hist-title">Camera History</div>
      <input class="hist-search" id="histSearch" type="text" placeholder="Filter by target…"
        oninput="histFilter()" autocomplete="off">
      <button class="btn btn-dim" onclick="histClearAll()" style="font-size:10px;">Clear All</button>
      <button class="modal-close" onclick="closeHistoryModal()">×</button>
    </div>
    <div class="hist-body" id="histBody">
      <div class="hist-empty" id="histEmpty">
        <div style="font-size:32px;opacity:0.2;">🌌</div>
        <div>No images captured yet</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px;">Images appear here after scheduled or manual exposures</div>
      </div>
    </div>
    <div class="hist-footer">
      <div class="hist-count" id="histCount">No images</div>
      <button class="btn btn-dim" onclick="histDownloadAll()" id="btnHistDlAll" style="font-size:10px;" disabled>Download All</button>
    </div>
  </div>
</div>

<!-- Image Lightbox -->
<div class="hist-lightbox hidden" id="histLightbox" onclick="if(event.target===this)closeLightbox()">
  <img class="hist-lb-img" id="histLbImg" src="" alt="Full image">
  <div class="hist-lb-meta" id="histLbMeta"></div>

  <!-- Inline metadata editor (hidden until Edit is clicked) -->
  <div class="hist-lb-edit hidden" id="histLbEdit">
    <div class="hist-lb-edit-row">
      <label>Target</label>
      <input id="metaTarget" class="hist-lb-edit-inp" type="text" placeholder="e.g. M42">
    </div>
    <div class="hist-lb-edit-row">
      <label>Exposure (s)</label>
      <input id="metaExpDur" class="hist-lb-edit-inp" type="number" step="0.01" min="0" placeholder="30">
    </div>
    <div class="hist-lb-edit-row">
      <label>Binning</label>
      <input id="metaBinning" class="hist-lb-edit-inp" type="number" step="1" min="1" placeholder="1">
    </div>
    <div class="hist-lb-edit-row">
      <label>Frame</label>
      <input id="metaFrame" class="hist-lb-edit-inp" type="number" step="1" min="1" placeholder="1">
    </div>
    <div class="hist-lb-edit-row">
      <label>Total</label>
      <input id="metaTotal" class="hist-lb-edit-inp" type="number" step="1" min="1" placeholder="1">
    </div>
    <div class="hist-lb-edit-actions">
      <button class="btn btn-dim" onclick="cancelMetaEdit()">Cancel</button>
      <button class="btn btn-green" onclick="saveMetaEdit()">Save</button>
    </div>
  </div>

  <div class="hist-lb-btns">
    <button class="btn btn-dim" onclick="closeLightbox()">Close</button>
    <button class="btn btn-dim" id="histLbEditBtn" onclick="openMetaEdit()">Edit Metadata</button>
    <a id="histLbDl" class="btn btn-green" download="image.png">Download</a>
  </div>
</div>

<!-- Schedule Modal -->
<div class="modal hidden sched-modal" id="schedModal" onclick="if(event.target===this)closeScheduleModal()">
  <div class="modal-content">

    <!-- Header -->
    <div class="sched-hdr">
      <span style="font-size:22px;flex-shrink:0;">🗓</span>
      <div class="sched-title">Night Schedule</div>
      <div style="display:flex;align-items:center;gap:7px;font-size:10px;color:var(--dim);letter-spacing:1px;flex-shrink:0;">
        <span>NIGHT</span>
        <input class="sched-night-inp" id="schedNightStart" type="time" value="20:00" onchange="schedRebuild()">
        <span style="color:var(--border);">→</span>
        <input class="sched-night-inp" id="schedNightEnd" type="time" value="05:00" onchange="schedRebuild()">
      </div>
      <button class="btn btn-green" onclick="schedOpenAdd(null)" style="flex-shrink:0;font-size:11px;">+ Add Observation</button>
      <button class="modal-close" onclick="closeScheduleModal()">×</button>
    </div>

    <!-- Sky conditions bar (shown when location is configured) -->
    <div class="sched-sky-bar" id="schedSkyBar"></div>

    <!-- Timeline -->
    <div class="sched-tl-wrap">
      <div class="sched-tl-labels" id="schedTlLabels"></div>
      <div class="sched-tl-track" id="schedTlTrack">
        <div class="sched-tl-now" id="schedTlNow" style="display:none;"></div>
      </div>
    </div>

    <!-- Live execution status (hidden when idle) -->
    <div class="sched-live" id="schedLive" style="display:none;">
      <div class="sched-live-pulse pulse"></div>
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <span class="sched-live-target" id="schedLiveTarget">—</span>
          <span class="sched-live-phase" id="schedLivePhase"></span>
          <span style="font-size:10px;color:var(--dim);" id="schedLiveFrames"></span>
          <span style="font-size:10px;color:var(--dim);" id="schedLiveProgress"></span>
        </div>
        <div class="sched-live-bar-wrap" style="margin-top:6px;">
          <div class="sched-live-bar" id="schedLiveBar" style="width:0%"></div>
        </div>
      </div>
      <button class="btn btn-red" onclick="schedAbort()" style="font-size:10px;padding:3px 10px;flex-shrink:0;">Abort</button>
    </div>

    <!-- Schedule list -->
    <div class="sched-body" id="schedBody">
      <div class="sched-empty" id="schedEmpty">
        <div style="font-size:32px;opacity:0.2;">🌙</div>
        <div>No observations scheduled yet</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px;">Click <strong style="color:var(--text);">+ Add Observation</strong> to start planning your night</div>
        <button class="btn btn-dim" onclick="schedAutoFill()" style="margin-top:12px;font-size:10px;">Auto-Fill Night</button>
      </div>
    </div>

    <!-- Footer -->
    <div class="sched-footer">
      <div class="sched-stats" id="schedStats">No observations planned</div>
      <button class="btn btn-dim" onclick="schedAutoFill()" style="font-size:10px;">Auto-Fill Night</button>
      <button class="btn btn-dim" onclick="schedClearAll()" style="font-size:10px;">Clear All</button>
      <button class="btn btn-blue" onclick="schedRunAll()" id="btnSchedRun">▶ Run Schedule</button>
    </div>
  </div>
</div>

<!-- Welcome / activation modal -->
<div class="modal hidden" id="welcomeModal">
  <div class="modal-content" style="max-width:420px;width:90%;padding:36px 32px 28px;text-align:center;">
    <div style="font-size:48px;margin-bottom:12px;">🌌</div>
    <div style="font-size:20px;font-weight:700;letter-spacing:0.5px;margin-bottom:8px;">Connect to The Telescope Net</div>
    <div style="font-size:13px;color:var(--dim);margin-bottom:28px;line-height:1.5;">
      Paste the activation code from The Telescope Net app to link this telescope to the network.
      <br><span style="font-size:11px;opacity:0.6;">App → Telescopes → Connect telescope</span>
    </div>
    <input id="welcomeCodeInput" class="inp" type="text"
      placeholder="BS-2024-XXXXXXXX"
      autocomplete="off" autocorrect="off" autocapitalize="characters" spellcheck="false"
      style="text-align:center;font-size:18px;letter-spacing:3px;text-transform:uppercase;width:100%;box-sizing:border-box;margin-bottom:14px;"
      oninput="this.value=this.value.toUpperCase()"
      onkeydown="if(event.key==='Enter')submitActivationCode()">
    <div id="welcomeErr" style="font-size:12px;color:var(--red,#e05252);min-height:18px;margin-bottom:10px;"></div>
    <button class="btn btn-green" id="btnWelcomeConnect"
      onclick="submitActivationCode()"
      style="width:100%;font-size:14px;padding:11px 0;letter-spacing:1px;">
      Connect
    </button>
    <div style="margin-top:18px;">
      <button onclick="closeWelcomeModal()" style="background:none;border:none;color:var(--dim);font-size:11px;cursor:pointer;text-decoration:underline;">
        Skip for now
      </button>
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
      <button id="cfgTab_cloud"      class="cfg-tab" onclick="switchCfgTab('cloud')">Cloud</button>
      <button id="cfgTab_advanced"   class="cfg-tab" onclick="switchCfgTab('advanced')">Advanced</button>
    </div>

    <!-- Form view -->
    <div id="cfgFormView" style="flex:1;overflow-y:auto;min-height:0;">

      <!-- SETUP -->
      <div id="cfgPanel_setup" class="cfg-panel">
        <div class="cfg-section-hdr">Observer Location</div>
        <div style="font-size:11px;color:var(--dim);margin-bottom:6px;">Your site coordinates — used for Alt-Az slewing, sun elevation, and dawn time calculations.</div>
        <div class="inp-group" style="margin-bottom:8px;">
          <div class="inp-label">Location name <span class="help-tip" data-tip="Type your observatory name, city, or address and click Look up. Coordinates are filled in automatically using OpenStreetMap.">?</span></div>
          <div style="display:flex;gap:6px;align-items:center;">
            <input class="inp" type="text" id="cfgObsLocationName" placeholder="e.g. Starfront Observatories, Rockwood TX" style="flex:1;">
            <button class="btn btn-sm" onclick="obsLookupLocation()" style="white-space:nowrap;padding:5px 12px;">Look up</button>
          </div>
          <div id="cfgObsLookupStatus" style="font-size:11px;color:var(--dim);margin-top:3px;min-height:14px;"></div>
        </div>
        <div class="cfg-field-grid">
          <div class="inp-group">
            <div class="inp-label">Latitude (° N) <span class="help-tip" data-tip="Decimal degrees north of the equator. Filled automatically by Look up, or enter manually. North is positive, South is negative.">?</span></div>
            <input class="inp" type="number" id="cfgObsLat" min="-90" max="90" step="0.0001" placeholder="e.g. 51.5074">
          </div>
          <div class="inp-group">
            <div class="inp-label">Longitude (° E) <span class="help-tip" data-tip="Decimal degrees east of Greenwich. Filled automatically by Look up, or enter manually. West is negative.">?</span></div>
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
        <div class="cfg-device-row">
          <label class="cfg-toggle" style="flex:1;">
            <input type="checkbox" id="cfgDevCoverEnabled">
            <span>Cover / Arm</span>
            <span class="help-tip" data-tip="Enable ALPACA CoverCalibrator for arm open/close control. Device 0 on the Seestar S50.">?</span>
          </label>
          <div class="inp-group" style="max-width:110px;">
            <div class="inp-label">Device # <span class="help-tip" data-tip="CoverCalibrator device index. Almost always 0 for the Seestar S50.">?</span></div>
            <input class="inp" type="number" id="cfgDevCoverNum" min="0" max="99" step="1">
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
            <div class="inp-label">Node ID <span class="help-tip" data-tip="Unique name for this observing node in The Telescope Net network. Used to label your data contributions. Example: node_001.">?</span></div>
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

            <!-- Cloud warning banner (hidden when no cloud concern) -->
            <div id="scanCloudWarning" style="display:none;margin-bottom:8px;padding:7px 10px;border-radius:6px;
                 background:rgba(192,132,252,0.12);border:1px solid #c084fc;font-size:10px;
                 color:#c084fc;line-height:1.5;">
              <strong>Possible cloud cover detected</strong> — purple spokes had no stars at the top
              altitude. Clouds produce the same zero-star result as a permanent obstruction but are
              temporary. Wait for a clear night and re-scan before applying the result.
              <div id="scanCloudDetail" style="margin-top:3px;opacity:.8;"></div>
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
      <!-- CLOUD -->
      <div id="cfgPanel_cloud" class="cfg-panel" style="display:none;">
        <div id="cfgCloudStatusBar" style="display:none;padding:8px 12px;border-radius:8px;font-size:12px;margin-bottom:14px;background:var(--glass-bg);border:1px solid var(--border);"></div>
        <label class="cfg-toggle cfg-toggle-lg">
          <input type="checkbox" id="cfgCloudEnabled">
          <span>Connect to The Telescope Net</span>
          <span class="help-tip" data-tip="Enable the cloud connection. The node will register, receive observation plans, and upload photometry results to the network.">?</span>
        </label>
        <div class="cfg-section-hdr">Connection</div>
        <div class="cfg-field-grid" style="grid-template-columns:1fr;">
          <div class="inp-group">
            <div class="inp-label">Cloud URL <span class="help-tip" data-tip="The URL of The Telescope Net cloud server. Leave as the default unless you are running a private instance.">?</span></div>
            <input class="inp" type="text" id="cfgCloudUrl" placeholder="https://api.thetelescope.net">
          </div>
          <div class="inp-group" id="cfgCloudCodeGroup">
            <div class="inp-label">Activation Code <span class="help-tip" data-tip="Your personal activation code from The Telescope Net app. Generate one under Telescopes → Connect telescope. Paste it here, save, then restart the node software to register.">?</span></div>
            <input class="inp" type="text" id="cfgCloudCode" placeholder="BS-2024-XXXXXXXX" style="text-transform:uppercase;letter-spacing:1px;">
            <div style="font-size:11px;color:var(--dim);margin-top:4px;">Get this code from The Telescope Net app → Telescopes → Connect telescope. After saving and restarting, this field will clear once registered.</div>
          </div>
        </div>
        <div class="cfg-section-hdr">Behavior</div>
        <div style="display:flex;flex-direction:column;gap:10px;">
          <label class="cfg-toggle">
            <input type="checkbox" id="cfgCloudAutoRun">
            <span>Auto-run plans from the cloud</span>
            <span class="help-tip" data-tip="When a new observation plan arrives from the network, start it automatically. If off, plans are downloaded but must be started manually from the Schedule panel.">?</span>
          </label>
        </div>
      </div>

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
        <div class="inp-group" style="margin-top:6px;">
          <div class="inp-label">Default Server <span class="help-tip" data-tip="If set, the discovery scan auto-connects to this server whenever it is found on the LAN, skipping the server list. Set automatically when you tick 'Set as default' during connection. Clear both fields to disable auto-connect.">?</span></div>
          <div style="display:flex;gap:6px;">
            <input class="inp" type="text"   id="cfgAlpacaDefaultAddr" placeholder="address  (e.g. 192.168.1.x)" style="flex:1">
            <input class="inp" type="number" id="cfgAlpacaDefaultPort" placeholder="port" min="1" max="65535" style="width:80px">
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

<!-- Log resize handle -->
<div class="resize-handle-h" id="logResizer"></div>

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
    <div id="srvConfirm" style="display:none;border-top:1px solid var(--border);padding-top:8px;margin-top:4px;">
      <div style="font-size:12px;color:var(--text);margin-bottom:6px;">Connect to <strong id="srvConfirmAddr"></strong>?</div>
      <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:8px;">
        <input type="checkbox" id="defaultCheckbox" style="margin-top:2px;accent-color:var(--blue);width:14px;height:14px;flex-shrink:0;cursor:pointer;">
        <label for="defaultCheckbox" style="color:var(--dim);font-size:11px;cursor:pointer;line-height:1.5;">Set as default — auto-connect when detected on LAN</label>
      </div>
      <div style="display:flex;gap:8px;">
        <button class="btn btn-green" id="btnSrvConnect" onclick="doConfirmedConnect()" style="flex:1" disabled>Connect</button>
        <button class="btn btn-dim" onclick="clearSrvSelection()">Back</button>
      </div>
    </div>
    <div class="sep"></div>
    <div style="color:var(--dim);font-size:11px;">Manual entry</div>
    <div class="inp-row">
      <input class="inp" id="mHost" placeholder="192.168.1.x" style="flex:1">
      <input class="inp" id="mPort" placeholder="11111" style="width:80px">
    </div>
    <div style="display:flex;align-items:flex-start;gap:8px;padding:10px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);margin-top:2px;">
      <input type="checkbox" id="permCheckbox" onchange="onPermCheckChange()" style="margin-top:2px;accent-color:var(--green);width:15px;height:15px;flex-shrink:0;cursor:pointer;">
      <label for="permCheckbox" style="color:var(--dim);font-size:11px;cursor:pointer;line-height:1.5;">I certify that I have permission to connect to this telescope server with the consent of the telescope owner.</label>
    </div>
    <div style="display:flex;gap:8px;">
      <button class="btn btn-green" id="btnManualConnect" onclick="doManualConnect()" style="flex:1" disabled>Connect</button>
      <button class="btn btn-dim" id="btnOverlayCancel" onclick="hideDiscover()" style="display:none">Cancel</button>
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
  _ensureObsLoc(s);
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
  // Panel is embedded in main grid — just ensure catalog is loaded
  loadCatalog();
  const el = document.getElementById("telModal");
  if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closeTelescopeModal() { /* panel is always visible when device is enabled */ }

function openCameraModal() {
  const el = document.getElementById("camModal");
  if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closeCameraModal() { /* panel is always visible when device is enabled */ }

// Close modals on escape key
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!document.getElementById("histLightbox").classList.contains("hidden")) { closeLightbox(); return; }
    const addWin = document.getElementById("schedAddWindow");
    if (addWin) { schedCloseAdd(); return; }
    closeConfigModal();
    closeScheduleModal();
    closeHistoryModal();
    if (_lastConnected) hideDiscover(); // only dismissable when connected
  }
});

// ── Header ──────────────────────────────────────────────────────────────────

let _overlayAutoShown = false;
let _lastConnected    = false;

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
    // Close blocking overlay now that we're connected
    document.getElementById("overlay").classList.add("hidden");
  } else {
    dot.className    = "dot dot-gray";
    label.textContent = "Disconnected";
    pill.classList.add("clickable");
    pill.onclick = showDiscover;
    document.getElementById("connDropdown").classList.remove("open");
    // Auto-show blocking overlay the first time we detect disconnected state
    if (!_overlayAutoShown) {
      _overlayAutoShown = true;
      showDiscover();
    }
  }

  _lastConnected = Boolean(s.connected);
  // Keep cancel button in sync with connection state
  const cancelBtn = document.getElementById("btnOverlayCancel");
  if (cancelBtn) cancelBtn.style.display = _lastConnected ? "" : "none";

  const errBanner = document.getElementById("errBanner");
  if (s.error) {
    errBanner.style.display = "block";
    errBanner.textContent   = "⚠ " + s.error;
    errBanner.title         = s.error;
  } else {
    errBanner.style.display = "none";
  }

  // Schedule button only visible when connected to an ALPACA server
  const schedBtn = document.getElementById("btnSchedule");
  if (schedBtn) schedBtn.style.display = s.connected ? "" : "none";
}

// ── Safety ──────────────────────────────────────────────────────────────────

function renderSafety(sf) {
  const pill   = document.getElementById("safetyPill");
  const dot    = document.getElementById("safetyDot");
  const label  = document.getElementById("safetyLabel");
  const reason = document.getElementById("safetyReason");
  const sunEl  = document.getElementById("sunEl");
  const resetBtn = document.getElementById("safetyResetBtn");

  if (!sf || sf.safe === undefined) { pill.style.display = "none"; return; }
  pill.style.display = "flex";

  if (sf.safe) {
    dot.className     = "dot dot-green";
    label.textContent = "SAFE";
    label.style.color = "var(--green)";
    reason.textContent = sf.heartbeat_ok ? "" : "hb?";
    if (resetBtn) resetBtn.style.display = "none";
  } else {
    dot.className     = "dot dot-red pulse";
    label.textContent = "UNSAFE";
    label.style.color = "var(--red)";
    reason.textContent = sf.reason ? `· ${sf.reason}` : "";
    if (resetBtn) resetBtn.style.display = "inline-block";
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

async function resetSafety() {
  const btn = document.getElementById("safetyResetBtn");
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    const r = await fetch("/api/safety/reset", { method: "POST" });
    const j = await r.json();
    if (!r.ok) { alert(j.error || "Safety reset failed"); }
    else { renderSafety(j); }
  } catch (e) {
    alert("Safety reset failed: " + e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Reset"; }
  }
}

// ── Telescope ───────────────────────────────────────────────────────────────

function renderTelescope(t) {
  const dotCls = t.connected ? "dot dot-green" : "dot dot-gray";
  document.getElementById("telModalDot").className = dotCls;

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
  document.getElementById("btnModalUnpark").disabled   = blocked || t.parked === false;
  document.getElementById("btnModalPark").disabled     = blocked || t.parked === true;
  document.getElementById("btnModalTrackOn").disabled  = blocked;
  document.getElementById("btnModalTrackOff").disabled = blocked;
  document.getElementById("btnModalSlew").disabled     = blocked;
  _joyBlocked = blocked;
  const pad = document.getElementById("joyPad");
  if (pad) {
    pad.style.opacity = blocked ? "0.35" : "1";
    pad.style.cursor  = blocked ? "not-allowed" : "grab";
  }

  // Arm (CoverCalibrator) controls
  const armGroup = document.getElementById("armCtrlGroup");
  const armLabel = document.getElementById("armStateLabel");
  const btnArmOpen  = document.getElementById("btnArmOpen");
  const btnArmClose = document.getElementById("btnArmClose");
  if (armGroup && t.arm_state != null) {
    armGroup.style.display = "";
    const ARM_NAMES = {0:"Not Present",1:"Closed",2:"Moving…",3:"Open",4:"Unknown",5:"Error"};
    const ARM_COLORS = {1:"var(--dim)",2:"var(--yellow)",3:"var(--green)",5:"var(--red)"};
    const armName = ARM_NAMES[t.arm_state] ?? "Unknown";
    if (armLabel) {
      armLabel.textContent = armName.toUpperCase();
      armLabel.style.color = ARM_COLORS[t.arm_state] ?? "var(--dim)";
    }
    const armBlocked = !t.connected || t.arm_busy || t.arm_state === 2;
    if (btnArmOpen)  btnArmOpen.disabled  = armBlocked || t.arm_state === 3;
    if (btnArmClose) btnArmClose.disabled = armBlocked || t.arm_state === 1;
  } else if (armGroup) {
    armGroup.style.display = "none";
  }

  updateSkyOverlay(t);

}

// ── Camera ───────────────────────────────────────────────────────────────────

const CAM_CLASSES = ["cs-idle","cs-wait","cs-expose","cs-read","cs-dl","cs-error"];

function renderCamera(c) {
  const dotCls = c.connected ? "dot dot-green" : "dot dot-gray";
  document.getElementById("camModalDot").className = dotCls;

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

  document.getElementById("btnModalExpose").disabled   = !c.connected || c.exposing;
  document.getElementById("btnModalAbortExp").disabled = !c.connected || !c.exposing;

  // ── Exposure countdown ring ────────────────────────────────────────────────
  const ring    = document.getElementById("expRing");
  const arc     = document.getElementById("expRingArc");
  const ringTxt = document.getElementById("expRingText");
  if (!ring) return;
  if (c.exposing && c.exposure_start_ts != null && c.exposure_duration != null) {
    ring.classList.add("active");
    const total = c.exposure_duration;
    const elapsed = (Date.now() / 1000) - c.exposure_start_ts;
    const frac = Math.min(1, elapsed / total);
    const remaining = Math.max(0, total - elapsed);
    const circ = 2 * Math.PI * 30;
    arc.style.strokeDashoffset = circ * frac;
    arc.style.stroke = frac > 0.85 ? "var(--green)" : "var(--yellow)";
    ringTxt.textContent = remaining < 1 ? "↓" : remaining.toFixed(0) + "s";
  } else {
    ring.classList.remove("active");
  }

}

// ── Night-vision toggle ───────────────────────────────────────────────────────

function toggleNightMode() {
  const html = document.documentElement;
  const on = html.hasAttribute("data-night");
  if (on) {
    html.removeAttribute("data-night");
    localStorage.removeItem("nightMode");
    document.getElementById("btnNightMode").textContent = "👁";
  } else {
    html.setAttribute("data-night", "");
    localStorage.setItem("nightMode", "1");
    document.getElementById("btnNightMode").textContent = "🔴";
  }
}
(function() {
  if (localStorage.getItem("nightMode") === "1") {
    document.documentElement.setAttribute("data-night", "");
    const b = document.getElementById("btnNightMode");
    if (b) b.textContent = "🔴";
  }
})();

// ── Astronomy math: RA/Dec → Alt/Az ──────────────────────────────────────────

let _obsLat = null, _obsLon = null;

function _ensureObsLoc(s) {
  const obs = s && s.config && s.config.safety && s.config.safety.observer;
  if (obs) {
    _obsLat = parseFloat(obs.latitude)  || 0;
    _obsLon = parseFloat(obs.longitude) || 0;
  }
}

function _raDecToAltAz(raHours, decDeg, latDeg, lonDeg, dateMs) {
  const D2R = Math.PI / 180;
  const d = new Date(dateMs || Date.now());
  const J2000 = 2451545.0;
  const jd = d / 86400000 + 2440587.5;
  const T  = (jd - J2000) / 36525;
  let gmst = 280.46061837 + 360.98564736629 * (jd - J2000)
           + 0.000387933 * T * T - T * T * T / 38710000;
  gmst = ((gmst % 360) + 360) % 360;
  const lst  = ((gmst + lonDeg) % 360 + 360) % 360;
  const ha   = (lst - raHours * 15 + 360) % 360;
  const haR  = ha  * D2R;
  const decR = decDeg * D2R;
  const latR = latDeg * D2R;
  const sinAlt = Math.sin(decR) * Math.sin(latR) + Math.cos(decR) * Math.cos(latR) * Math.cos(haR);
  const alt    = Math.asin(Math.max(-1, Math.min(1, sinAlt))) / D2R;
  const cosAz  = (Math.sin(decR) - Math.sin(latR) * sinAlt) / (Math.cos(latR) * Math.cos(alt * D2R));
  let az = Math.acos(Math.max(-1, Math.min(1, cosAz))) / D2R;
  if (Math.sin(haR) > 0) az = 360 - az;
  return { alt, az };
}

// ── Live sky overlay on horizon canvas ───────────────────────────────────────

let _telSkyRA = null, _telSkyDec = null;

function updateSkyOverlay(t) {
  if (_obsLat === null) return;
  const canvas = document.getElementById("skyCanvas");
  if (!canvas) return;
  if (t.connected && t.ra != null) {
    _telSkyRA  = t.ra;
    _telSkyDec = t.dec;
  } else {
    _telSkyRA  = null;
    _telSkyDec = null;
  }
}

function drawSkyOverlay(ctx, CX, CY, MAX_R) {
  if (_obsLat === null) return;
  const now = Date.now();

  // Draw scheduled targets
  if (typeof _schedule !== "undefined") {
    _schedule.forEach(function(item, i) {
      if (item.ra == null || item.dec == null) return;
      const aa = _raDecToAltAz(item.ra, item.dec, _obsLat, _obsLon, now);
      if (aa.alt < 0) return;
      const r  = MAX_R * (1 - aa.alt / 90);
      const az = aa.az * Math.PI / 180;
      const x  = CX + r * Math.sin(az);
      const y  = CY - r * Math.cos(az);
      const colors = ["#00e676","#448aff","#ffd740","#bc83ff","#ff7f50","#40dcdc"];
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, 2 * Math.PI);
      ctx.fillStyle = colors[i % colors.length];
      ctx.globalAlpha = 0.85;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.font = "9px monospace";
      ctx.fillStyle = colors[i % colors.length];
      ctx.textAlign = "left";
      ctx.fillText(item.target.slice(0, 12), x + 7, y + 3);
    });
  }

  // Draw telescope pointing position
  if (_telSkyRA !== null && _telSkyDec !== null) {
    const aa = _raDecToAltAz(_telSkyRA, _telSkyDec, _obsLat, _obsLon, now);
    if (aa.alt >= 0) {
      const r  = MAX_R * (1 - aa.alt / 90);
      const az = aa.az * Math.PI / 180;
      const x  = CX + r * Math.sin(az);
      const y  = CY - r * Math.cos(az);
      const sz = 10;
      ctx.save();
      ctx.strokeStyle = "#00e676";
      ctx.lineWidth   = 2;
      ctx.shadowBlur  = 8;
      ctx.shadowColor = "#00e676";
      ctx.beginPath(); ctx.moveTo(x - sz, y); ctx.lineTo(x + sz, y); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x, y - sz); ctx.lineTo(x, y + sz); ctx.stroke();
      ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI); ctx.stroke();
      ctx.restore();
    }
  }
}

// ── Altitude sparklines in schedule items ─────────────────────────────────────

function drawAltSparkline(canvas, raHours, decDeg, nightStartH, nightEndH) {
  if (_obsLat === null || canvas == null) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const steps = 48;
  const dur   = nightEndH < nightStartH ? (nightEndH + 24 - nightStartH) : (nightEndH - nightStartH);
  const pts   = [];

  for (let i = 0; i <= steps; i++) {
    const frac    = i / steps;
    const hourOff = nightStartH + frac * dur;
    const ms      = today.getTime() + hourOff * 3600000;
    const aa      = _raDecToAltAz(raHours, decDeg, _obsLat, _obsLon, ms);
    pts.push({ frac, alt: aa.alt });
  }

  // Background
  ctx.fillStyle = "rgba(0,0,0,0.25)";
  ctx.fillRect(0, 0, W, H);

  // Altitude curve
  ctx.beginPath();
  let started = false;
  pts.forEach(function(p) {
    const x = p.frac * W;
    const y = H - Math.max(0, Math.min(H, (p.alt / 90) * H));
    if (!started) { ctx.moveTo(x, y); started = true; }
    else           { ctx.lineTo(x, y); }
  });
  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.lineWidth = 1;
  ctx.stroke();

  // Colored fill based on altitude band
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0,    "rgba(0,230,118,0.35)");
  grad.addColorStop(0.22, "rgba(0,230,118,0.28)");
  grad.addColorStop(0.5,  "rgba(255,215,64,0.25)");
  grad.addColorStop(1,    "rgba(255,82,82,0.15)");
  ctx.beginPath();
  pts.forEach(function(p, i) {
    const x = p.frac * W;
    const y = H - Math.max(0, Math.min(H, (p.alt / 90) * H));
    if (i === 0) ctx.moveTo(x, H);
    ctx.lineTo(x, y);
  });
  ctx.lineTo(W, H);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // 20° line
  const y20 = H - (20 / 90) * H;
  ctx.beginPath();
  ctx.moveTo(0, y20); ctx.lineTo(W, y20);
  ctx.strokeStyle = "rgba(255,215,64,0.25)";
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 3]);
  ctx.stroke();
  ctx.setLineDash([]);
}

// ── Main area visibility ──────────────────────────────────────────────────────

function updateImgRow() {
  const tel   = document.getElementById("telModal");
  const cam   = document.getElementById("camModal");
  const pier  = document.getElementById("pierCamSub");
  const empty = document.getElementById("mainEmpty");
  const hasContent = [tel, cam, pier].some(el => el && !el.classList.contains("hidden"));
  empty.style.display = hasContent ? "none" : "flex";
}

// ── Image ────────────────────────────────────────────────────────────────────

let _lastImageId = -1;

async function renderImage(s) {
  if (!s.image_captured) return;
  if (s.image_id === _lastImageId) return;
  _lastImageId = s.image_id;

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

async function apiArmOpen() {
  try { await fetch("/api/arm/open", { method: "POST" }); }
  catch (e) { alert("Arm open failed: " + e.message); }
}

async function apiArmClose() {
  try { await fetch("/api/arm/close", { method: "POST" }); }
  catch (e) { alert("Arm close failed: " + e.message); }
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

const STAR_CATALOG = [

  // Named Stars
  {id:"Sirius",        name:"α Canis Majoris",   type:"Star", ra:6.7526,  dec:-16.716},
  {id:"Canopus",       name:"α Carinae",          type:"Star", ra:6.3992,  dec:-52.696},
  {id:"Arcturus",      name:"α Boötis",           type:"Star", ra:14.2611, dec:19.182 },
  {id:"Alpha Centauri",name:"α Centauri",         type:"Star", ra:14.6597, dec:-60.834},
  {id:"Vega",          name:"α Lyrae",            type:"Star", ra:18.6156, dec:38.784 },
  {id:"Capella",       name:"α Aurigae",          type:"Star", ra:5.2781,  dec:45.998 },
  {id:"Rigel",         name:"β Orionis",          type:"Star", ra:5.2422,  dec:-8.202 },
  {id:"Procyon",       name:"α Canis Minoris",    type:"Star", ra:7.6553,  dec:5.225  },
  {id:"Achernar",      name:"α Eridani",          type:"Star", ra:1.6286,  dec:-57.237},
  {id:"Betelgeuse",    name:"α Orionis",          type:"Star", ra:5.9194,  dec:7.407  },
  {id:"Hadar",         name:"β Centauri",         type:"Star", ra:14.0639, dec:-60.373},
  {id:"Altair",        name:"α Aquilae",          type:"Star", ra:19.8464, dec:8.868  },
  {id:"Acrux",         name:"α Crucis",           type:"Star", ra:12.4433, dec:-63.099},
  {id:"Aldebaran",     name:"α Tauri",            type:"Star", ra:4.5986,  dec:16.509 },
  {id:"Antares",       name:"α Scorpii",          type:"Star", ra:16.4901, dec:-26.432},
  {id:"Spica",         name:"α Virginis",         type:"Star", ra:13.4199, dec:-11.161},
  {id:"Pollux",        name:"β Geminorum",        type:"Star", ra:7.7553,  dec:28.026 },
  {id:"Fomalhaut",     name:"α Piscis Austrini",  type:"Star", ra:22.9608, dec:-29.622},
  {id:"Deneb",         name:"α Cygni",            type:"Star", ra:20.6906, dec:45.280 },
  {id:"Mimosa",        name:"β Crucis",           type:"Star", ra:12.7953, dec:-59.689},
  {id:"Regulus",       name:"α Leonis",           type:"Star", ra:10.1394, dec:11.967 },
  {id:"Adhara",        name:"ε Canis Majoris",    type:"Star", ra:6.9772,  dec:-28.972},
  {id:"Castor",        name:"α Geminorum",        type:"Star", ra:7.5767,  dec:31.888 },
  {id:"Gacrux",        name:"γ Crucis",           type:"Star", ra:12.5194, dec:-57.113},
  {id:"Bellatrix",     name:"γ Orionis",          type:"Star", ra:5.4186,  dec:6.350  },
  {id:"Elnath",        name:"β Tauri",            type:"Star", ra:5.4381,  dec:28.608 },
  {id:"Alnilam",       name:"ε Orionis",          type:"Star", ra:5.6036,  dec:-1.202 },
  {id:"Alnitak",       name:"ζ Orionis",          type:"Star", ra:5.6797,  dec:-1.943 },
  {id:"Mintaka",       name:"δ Orionis",          type:"Star", ra:5.5333,  dec:-0.299 },
  {id:"Saiph",         name:"κ Orionis",          type:"Star", ra:5.7958,  dec:-9.670 },
  {id:"Alioth",        name:"ε Ursae Majoris",    type:"Star", ra:12.9006, dec:55.960 },
  {id:"Dubhe",         name:"α Ursae Majoris",    type:"Star", ra:11.0622, dec:61.751 },
  {id:"Merak",         name:"β Ursae Majoris",    type:"Star", ra:11.0306, dec:56.383 },
  {id:"Phecda",        name:"γ Ursae Majoris",    type:"Star", ra:11.8972, dec:53.695 },
  {id:"Megrez",        name:"δ Ursae Majoris",    type:"Star", ra:12.2569, dec:57.033 },
  {id:"Mizar",         name:"ζ Ursae Majoris",    type:"Star", ra:13.3986, dec:54.925 },
  {id:"Alkaid",        name:"η Ursae Majoris",    type:"Star", ra:13.7923, dec:49.313 },
  {id:"Polaris",       name:"α Ursae Minoris",    type:"Star", ra:2.5303,  dec:89.264 },
  {id:"Mirfak",        name:"α Persei",           type:"Star", ra:3.4053,  dec:49.861 },
  {id:"Algol",         name:"β Persei",           type:"Star", ra:3.1364,  dec:40.957 },
  {id:"Alpheratz",     name:"α Andromedae",       type:"Star", ra:0.1397,  dec:29.090 },
  {id:"Mirach",        name:"β Andromedae",       type:"Star", ra:1.1622,  dec:35.621 },
  {id:"Almach",        name:"γ Andromedae",       type:"Star", ra:2.0650,  dec:42.330 },
  {id:"Schedar",       name:"α Cassiopeiae",      type:"Star", ra:0.6753,  dec:56.537 },
  {id:"Alderamin",     name:"α Cephei",           type:"Star", ra:21.3097, dec:62.585 },
  {id:"Hamal",         name:"α Arietis",          type:"Star", ra:2.1197,  dec:23.462 },
  {id:"Denebola",      name:"β Leonis",           type:"Star", ra:11.8178, dec:14.572 },
  {id:"Algieba",       name:"γ Leonis",           type:"Star", ra:10.3319, dec:19.842 },
  {id:"Alphard",       name:"α Hydrae",           type:"Star", ra:9.4597,  dec:-8.659 },
  {id:"Cor Caroli",    name:"α Canum Venaticorum",type:"Star", ra:12.9331, dec:38.318 },
  {id:"Izar",          name:"ε Boötis",           type:"Star", ra:14.7492, dec:27.074 },
  {id:"Alphecca",      name:"α Coronae Borealis", type:"Star", ra:15.5783, dec:26.715 },
  {id:"Rasalhague",    name:"α Ophiuchi",         type:"Star", ra:17.5822, dec:12.560 },
  {id:"Nunki",         name:"σ Sagittarii",       type:"Star", ra:18.9211, dec:-26.296},
  {id:"Kaus Australis",name:"ε Sagittarii",       type:"Star", ra:18.4031, dec:-34.385},
  {id:"Shaula",        name:"λ Scorpii",          type:"Star", ra:17.5603, dec:-37.104},
  {id:"Sargas",        name:"θ Scorpii",          type:"Star", ra:17.6217, dec:-42.998},
  {id:"Atria",         name:"α Trianguli Aust.",  type:"Star", ra:16.8111, dec:-69.028},
  {id:"Peacock",       name:"α Pavonis",          type:"Star", ra:20.4275, dec:-56.735},
  {id:"Miaplacidus",   name:"β Carinae",          type:"Star", ra:9.2200,  dec:-69.717},
  {id:"Avior",         name:"ε Carinae",          type:"Star", ra:8.3750,  dec:-59.510},
];

// Combined catalog: stars (hardcoded) + DSOs (fetched from /api/catalog)
let _catalog    = null;   // null = not loaded yet
let _catalogIdx = -1;

async function loadCatalog() {
  if (_catalog !== null) return;
  try {
    const r    = await fetch("/api/catalog");
    const dsos = await r.json();
    _catalog = STAR_CATALOG.concat(dsos);
  } catch (e) {
    console.warn("Catalog fetch failed, using stars only:", e);
    _catalog = STAR_CATALOG.slice();
  }
}

function _catalogScore(o, tokens, q) {
  const id   = o.id.toLowerCase();
  const name = (o.name || "").toLowerCase();
  const type = o.type.toLowerCase();
  const full = id + " " + name + " " + type;

  if (!tokens.every(t => full.includes(t))) return -1;

  let score = 0;
  if (id === q)                                         score += 100;  // exact id
  else if (id.startsWith(q))                            score += 70;   // id prefix
  if (name && name.startsWith(q))                       score += 60;   // name prefix
  if (name && !name.startsWith(q) && name.includes(q)) score += 20;   // name contains

  // Catalog tier bonus so Messier > stars > NGC > IC within the same query score
  if (/^m\d+$/.test(id))   score += 8;
  else if (o.type === "Star") score += 5;
  else if (id.startsWith("ngc")) score += 2;

  return score;
}

function catalogFilter() {
  const input = document.getElementById("catalogSearch");
  const q     = input.value.trim().toLowerCase();
  const dd    = document.getElementById("catalogDropdown");
  _catalogIdx = -1;

  if (q.length < 2) { dd.style.display = "none"; return; }

  const src    = _catalog || STAR_CATALOG;
  const tokens = q.split(/\s+/).filter(Boolean);

  const matches = src
    .map(o => ({ o, s: _catalogScore(o, tokens, q) }))
    .filter(x => x.s >= 0)
    .sort((a, b) => b.s - a.s)
    .slice(0, 30)
    .map(x => x.o);

  if (matches.length === 0) { dd.style.display = "none"; return; }

  const r = input.getBoundingClientRect();
  dd.style.top   = (r.bottom + 2) + "px";
  dd.style.left  = r.left + "px";
  dd.style.width = r.width + "px";

  const loading = _catalog === null
    ? `<div style="padding:6px 10px;font-size:10px;color:var(--dim);letter-spacing:1px;">LOADING CATALOG…</div>`
    : "";

  dd.innerHTML = loading + matches.map((o, i) => {
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

let _lastSlewPayload = null;

function _hideSlewRejection() {
  document.getElementById("slewRejectionBanner").style.display = "none";
  document.getElementById("slewRejectionMsg").textContent = "";
}

function _showSlewRejection(msg) {
  document.getElementById("slewRejectionMsg").textContent = msg;
  document.getElementById("slewRejectionBanner").style.display = "block";
}

async function _doSlew(payload) {
  const btn = document.getElementById("btnModalSlew");
  btn.disabled = true; btn.textContent = "Slewing…";
  try {
    const r = await fetch("/api/slew", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (r.status === 403 && (d.horizon_blocked || d.blocked || d.unsafe)) {
      _lastSlewPayload = payload;
      let notice = d.error || "Slew rejected";
      if (d.min_safe_alt != null)
        notice += `  (min safe altitude in this direction: ${d.min_safe_alt}°)`;
      _showSlewRejection(notice);
    } else {
      _hideSlewRejection();
      if (!d.ok) alert(d.error || "Slew failed");
    }
  } catch (e) { alert("Slew failed: " + e.message); }
  btn.textContent = "Slew to Target";
  // disabled state re-evaluated on next poll
}

async function apiSlew() {
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
  _hideSlewRejection();
  await _doSlew(payload);
}

async function apiForceSlew() {
  if (!_lastSlewPayload) return;
  const btn = document.getElementById("btnForceSlew");
  btn.disabled = true; btn.textContent = "Forcing…";
  await _doSlew({ ..._lastSlewPayload, force: true });
  btn.disabled = false; btn.textContent = "⚠ Force Slew — I accept the risks";
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

      // Colour-code each spoke by status.
      // cloud_suspect (amber) = no stars at the top altitude — likely clouds, not a wall.
      // done + horizon_alt=0 (green) = clear to the floor.
      // done + horizon_alt>0 (orange-red) = real obstruction found.
      for (let i = 0; i < N; i++) {
        const d   = dirs[i];
        const az  = i * AZ_STEP;
        const a   = az * Math.PI / 180;
        const [ex, ey] = toXY(0, az);
        const col = d.status === 'scanning'
                  ? '#f0c040'
                  : d.status === 'done' && d.cloud_suspect
                  ? '#c084fc'   // purple = cloud-suspected reading
                  : d.status === 'done'
                  ? (d.horizon_alt === 0 ? '#3fb950' : '#e06030')
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
            // Purple dot for cloud-suspect positions (no stars at top altitude)
            ctx.fillStyle = d.cloud_suspect ? '#c084fc'
                          : clear           ? '#56d364'
                          : hasSky          ? '#e06030'
                          :                   '#888';
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
          ctx.fillStyle     = d.status === 'scanning' ? '#f0c040'
                            : d.cloud_suspect         ? '#c084fc'
                            : d.horizon_alt === 0     ? '#3fb950'
                            :                           '#e06030';
          ctx.textAlign     = "center";
          ctx.textBaseline  = "middle";
          const rimLbl = d.status === 'scanning' ? '…'
                       : d.cloud_suspect          ? DIR_LABELS[i] + ' ?'
                       : d.horizon_alt === 0      ? DIR_LABELS[i] + ' ✓'
                       :                            DIR_LABELS[i] + ' ' + d.horizon_alt + '°';
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

    // Telescope pointing overlay + scheduled targets
    drawSkyOverlay(ctx, CX, CY, MAX_R);
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

  function _setScanUI(running, hasResult, cloudAbort) {
    const start   = _scanEl("btnStartScan");
    const cancel  = _scanEl("btnCancelScan");
    const apply   = _scanEl("btnApplyScan");
    const fields  = _scanEl("scanConfigFields");
    const prog    = _scanEl("scanProgress");
    if (start)  start.style.display  = running || hasResult || cloudAbort ? "none" : "";
    if (cancel) cancel.style.display = running ? "" : "none";
    // Block Apply when the scan was aborted due to clouds
    if (apply)  apply.style.display  = (!running && hasResult && !cloudAbort) ? "" : "none";
    if (fields) fields.style.opacity = running ? "0.4" : "1";
    if (prog)   prog.style.display   = (running || hasResult || cloudAbort) ? "" : "none";
    // Show a Rescan button after a cloud abort so the user can try again
    let rescanBtn = _scanEl("btnRescanAfterCloud");
    if (!rescanBtn && cloudAbort) {
      rescanBtn = document.createElement("button");
      rescanBtn.id = "btnRescanAfterCloud";
      rescanBtn.className = "btn";
      rescanBtn.style.cssText = "flex:1;background:#c084fc;color:#fff;";
      rescanBtn.textContent = "Try Again";
      rescanBtn.onclick = function () {
        rescanBtn.remove();
        window.setScanOverlay(null);
        _setScanUI(false, false, false);
        const lbl = _scanEl("scanProgressLabel");
        if (lbl) { lbl.textContent = ""; }
        const warn = _scanEl("scanCloudWarning");
        if (warn) warn.style.display = "none";
        const prog2 = _scanEl("scanProgress");
        if (prog2) prog2.style.display = "none";
        const s2 = _scanEl("btnStartScan");
        if (s2) s2.style.display = "";
      };
      const btnRow = start && start.parentElement;
      if (btnRow) btnRow.appendChild(rescanBtn);
    } else if (rescanBtn && !cloudAbort) {
      rescanBtn.remove();
    }
  }

  function _updateProgressUI(state) {
    const label       = _scanEl("scanProgressLabel");
    const bars        = _scanEl("scanDirectionBars");
    const cloudBanner = _scanEl("scanCloudWarning");
    const cloudDetail = _scanEl("scanCloudDetail");
    if (!label || !bars || !state.directions) return;

    // Status line
    const scanning = state.directions.find(d => d.status === "scanning");
    const done     = state.directions.filter(d => d.status === "done").length;
    if (state.running && scanning) {
      label.textContent = `Scanning ${DIR_LABELS[state.directions.indexOf(scanning)]} (${done}/12)…`;
      label.style.color = "var(--blue)";
    } else if (!state.running && state.cloud_abort) {
      label.textContent = "Scan aborted — " + (state.error || "cloud cover suspected");
      label.style.color = "#c084fc";
    } else if (!state.running && state.error) {
      label.textContent = "Scan failed: " + state.error;
      label.style.color = "var(--red)";
    } else if (!state.running && state.result) {
      const warn = state.cloud_warning ? " (cloud warning — see below)" : "";
      label.textContent = "Scan complete — review the dashed orange profile, then Apply." + warn;
      label.style.color = state.cloud_warning ? "#c084fc" : "var(--green)";
    }

    // Cloud warning banner
    if (cloudBanner) {
      const showBanner = state.cloud_warning || state.cloud_abort;
      cloudBanner.style.display = showBanner ? "" : "none";
      if (showBanner && cloudDetail) {
        const nd = state.cloud_dirs || 0;
        const total = state.directions.length;
        cloudDetail.textContent = `${nd} of ${total} directions returned zero stars at the highest scanned altitude.`;
      }
    }

    // Per-direction bar chart
    bars.innerHTML = "";
    state.directions.forEach(function (d, i) {
      const bar = document.createElement("div");
      const tipSuffix = d.cloud_suspect ? " (cloud?)" : (d.horizon_alt != null ? "  " + d.horizon_alt + "°" : "");
      bar.title = DIR_LABELS[i] + tipSuffix;
      const heightPct = d.status === "done"
        ? Math.max(4, Math.round((d.horizon_alt / 90) * 100))
        : (d.status === "scanning" ? 50 : 4);
      const bg = d.status === "done" && d.cloud_suspect ? "#c084fc"
               : d.status === "done"    ? (d.horizon_alt === 0 ? "var(--green)" : "#e06030")
               : d.status === "scanning" ? "#f0c040"
               :                           "var(--border)";
      bar.style.cssText = [
        "flex:1", "border-radius:2px 2px 0 0", "transition:height .3s",
        "height:" + heightPct + "%",
        "background:" + bg,
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
      _setScanUI(s.running, !!s.result, !!s.cloud_abort);
      if (!s.running) {
        clearInterval(_pollTimer); _pollTimer = null;
        _scanResult = (!s.cloud_abort && s.result) ? s.result : null;
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
    _setScanUI(true, false, false);
    try {
      const r = await fetch("/api/safety/horizon-scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
      });
      const d = await r.json();
      if (!d.ok) {
        alert("Scan failed to start: " + (d.error || "unknown"));
        _setScanUI(false, false, false); return;
      }
    } catch (e) {
      alert("Scan failed to start: " + e.message);
      _setScanUI(false, false, false); return;
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
    _setScanUI(false, false, false);
    const label = _scanEl("scanProgressLabel");
    if (label) { label.textContent = "Scan cancelled."; label.style.color = "var(--dim)"; }
    const warn = _scanEl("scanCloudWarning");
    if (warn) warn.style.display = "none";
  };

  window.applyHorizonScanResult = function () {
    if (!_scanResult) return;
    window.loadAltsFromResult(_scanResult);
    _scanResult = null;
    _setScanUI(false, false, false);
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
      if (s.running || s.result || s.cloud_abort) {
        window.setScanOverlay(s);
        _updateProgressUI(s);
        _setScanUI(s.running, !!s.result, !!s.cloud_abort);
        _scanResult = (!s.cloud_abort && s.result) ? s.result : null;
        if (s.running) _pollTimer = setInterval(_poll, 1500);
      }
    }).catch(function () {});
  });
})();

// ── Discovery overlay ────────────────────────────────────────────────────────

function onPermCheckChange() {
  const checked = document.getElementById("permCheckbox").checked;
  document.getElementById("btnManualConnect").disabled = !checked;
  if (_selectedSrv) document.getElementById("btnSrvConnect").disabled = !checked;
}

function showDiscover() {
  const overlay = document.getElementById("overlay");
  overlay.classList.remove("hidden");
  // Reset permission checkbox each time overlay opens
  document.getElementById("permCheckbox").checked = false;
  onPermCheckChange();
  clearSrvSelection();
  // Show cancel button only when already connected
  document.getElementById("btnOverlayCancel").style.display = _lastConnected ? "" : "none";
  doScan();
}

function hideDiscover() {
  if (!_lastConnected) return; // cannot dismiss before connecting
  document.getElementById("overlay").classList.add("hidden");
}

async function doScan() {
  const btn = document.getElementById("scanBtn");
  btn.textContent = "Scanning…"; btn.disabled = true;
  document.getElementById("srvList").innerHTML = "";
  clearSrvSelection();
  try {
    const r    = await fetch("/api/discover", { method: "POST" });
    const data = await r.json();
    const list = document.getElementById("srvList");
    const defSrv = data.default_server;

    // Auto-connect if the saved default server is present on the LAN
    if (defSrv && data.servers?.some(s => s.address === defSrv.address && s.port === defSrv.port)) {
      list.innerHTML = `<div style="color:var(--dim);font-size:12px;padding:4px 0;">Auto-connecting to saved server ${defSrv.address}:${defSrv.port}…</div>`;
      btn.textContent = "Scan LAN for servers"; btn.disabled = false;
      await connectTo(defSrv.address, defSrv.port, false, true);
      return;
    }

    if (data.servers?.length) {
      data.servers.forEach(srv => {
        const item = document.createElement("div");
        item.className        = "srv-item";
        item.textContent      = `${srv.address}:${srv.port}`;
        item.onclick = () => selectSrv(srv.address, srv.port);
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
  if (!document.getElementById("permCheckbox").checked) return;
  const host = document.getElementById("mHost").value.trim();
  const port = parseInt(document.getElementById("mPort").value.trim() || "11111");
  if (!host) return;
  await connectTo(host, port);
}

let _selectedSrv = null;

function selectSrv(host, port) {
  _selectedSrv = { host, port };
  document.querySelectorAll(".srv-item").forEach(el => el.classList.remove("selected"));
  document.querySelectorAll(".srv-item").forEach(el => {
    if (el.textContent === `${host}:${port}`) el.classList.add("selected");
  });
  document.getElementById("srvConfirmAddr").textContent = `${host}:${port}`;
  document.getElementById("defaultCheckbox").checked = false;
  const permOk = document.getElementById("permCheckbox").checked;
  document.getElementById("btnSrvConnect").disabled = !permOk;
  document.getElementById("srvConfirm").style.display = "";
}

function clearSrvSelection() {
  _selectedSrv = null;
  document.querySelectorAll(".srv-item").forEach(el => el.classList.remove("selected"));
  const confirm = document.getElementById("srvConfirm");
  if (confirm) confirm.style.display = "none";
}

async function doConfirmedConnect() {
  if (!_selectedSrv || !document.getElementById("permCheckbox").checked) return;
  const setDefault = document.getElementById("defaultCheckbox").checked;
  await connectTo(_selectedSrv.host, _selectedSrv.port, setDefault);
}

async function connectTo(host, port, setAsDefault = false, skipPermCheck = false) {
  if (!skipPermCheck && !document.getElementById("permCheckbox").checked) return;
  document.getElementById("overlay").classList.add("hidden");
  try {
    const r = await fetch("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, port, set_as_default: setAsDefault }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "failed");
  } catch (e) {
    alert("Connection failed: " + e.message);
    // If connect failed and not connected, re-show blocking overlay
    if (!_lastConnected) showDiscover();
  }
}

// ── Config editor ─────────────────────────────────────────────────────────────

let _cfgView      = 'form';
let _cfgParsed    = {};
let _cfgActiveTab = 'setup';
const _CFG_TABS   = ['setup','photometry','aavso','safety','cloud','advanced'];

function switchCfgTab(tab) {
  _cfgActiveTab = tab;
  _CFG_TABS.forEach(t => {
    const panel = document.getElementById('cfgPanel_' + t);
    const btn   = document.getElementById('cfgTab_'   + t);
    if (panel) panel.style.display = (t === tab) ? '' : 'none';
    if (btn)   btn.classList.toggle('active', t === tab);
  });
  if (tab === 'horizon') window.loadSkyMask();
  if (tab === 'cloud') _loadCloudRegistrationStatus();
}

async function _loadCloudRegistrationStatus() {
  const bar = document.getElementById('cfgCloudStatusBar');
  const codeGroup = document.getElementById('cfgCloudCodeGroup');
  if (!bar) return;
  try {
    const [cloudResp, cfgResp] = await Promise.all([
      fetch('/api/cloud'),
      fetch('/api/config/parsed').catch(() => null),
    ]);
    if (!cloudResp.ok) { bar.style.display = 'none'; return; }
    const d = await cloudResp.json();
    const cfg = cfgResp && cfgResp.ok ? await cfgResp.json() : {};
    const savedCode = !_isPlaceholderCode(((cfg.cloud || {}).activation_code || '').trim());
    bar.style.display = '';
    if (d.registered) {
      bar.style.borderColor = 'var(--green)';
      bar.innerHTML = '<span style="color:var(--green-hi)">&#9679; Registered</span>'
        + ' &mdash; Node ID: <code style="font-size:11px">' + (d.node_id || '') + '</code>'
        + (d.last_heartbeat_ok ? ' &mdash; <span style="color:var(--green-hi)">heartbeat OK</span>' : '');
      if (codeGroup) codeGroup.style.display = 'none';
    } else if (d.error) {
      bar.style.borderColor = 'var(--red)';
      bar.innerHTML = '<span style="color:var(--red)">&#9679; Registration failed</span>'
        + ' &mdash; <span style="color:var(--dim)">' + String(d.error).replace(/[<>&]/g, '') + '</span>';
      if (codeGroup) codeGroup.style.display = '';
    } else if (savedCode) {
      bar.style.borderColor = 'var(--warn, #f5a623)';
      bar.innerHTML = '<span style="color:var(--warn, #f5a623)">&#9679; Activation code saved</span>'
        + ' &mdash; restart the node software to complete registration.';
      if (codeGroup) codeGroup.style.display = '';
    } else {
      bar.style.borderColor = 'var(--warn, #f5a623)';
      bar.innerHTML = '<span style="color:var(--warn, #f5a623)">&#9679; Not registered</span>'
        + ' &mdash; paste your activation code from The Telescope Net app below.';
      if (codeGroup) codeGroup.style.display = '';
    }
  } catch (_) { bar.style.display = 'none'; }
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
  const _defSrv = _cfgGet(c, 'alpaca.default_server', null);
  setVal('cfgAlpacaDefaultAddr', _defSrv ? _defSrv.address : '');
  setVal('cfgAlpacaDefaultPort', _defSrv ? _defSrv.port    : '');
  setVal('cfgObsLocationName', _cfgGet(c, 'observatory.name', ''));
  setVal('cfgObsLat',        _cfgGet(c, 'safety.observer.latitude',  null) ?? _cfgGet(c, 'observatory.latitude', 0.0));
  setVal('cfgObsLon',        _cfgGet(c, 'safety.observer.longitude', null) ?? _cfgGet(c, 'observatory.longitude', 0.0));
  setVal('cfgDevTelEnabled', _cfgGet(c, 'devices.telescope.enabled',         false));
  setVal('cfgDevTelNum',     _cfgGet(c, 'devices.telescope.device_number',   0));
  setVal('cfgDevCamEnabled', _cfgGet(c, 'devices.camera.enabled',            false));
  setVal('cfgDevCamNum',     _cfgGet(c, 'devices.camera.device_number',      0));
  setVal('cfgDevFocEnabled', _cfgGet(c, 'devices.focuser.enabled',           false));
  setVal('cfgDevFocNum',     _cfgGet(c, 'devices.focuser.device_number',     0));
  setVal('cfgDevFwEnabled',    _cfgGet(c, 'devices.filterwheel.enabled',          false));
  setVal('cfgDevFwNum',        _cfgGet(c, 'devices.filterwheel.device_number',    0));
  setVal('cfgDevCoverEnabled', _cfgGet(c, 'devices.covercalibrator.enabled',      false));
  setVal('cfgDevCoverNum',     _cfgGet(c, 'devices.covercalibrator.device_number', 0));
  setVal('cfgTrackingRate',    _cfgGet(c, 'telescope.tracking_rate', 0));
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
  setVal('cfgCloudEnabled', _cfgGet(c, 'cloud.enabled',           false));
  setVal('cfgCloudUrl',     _cfgGet(c, 'cloud.url',               ''));
  setVal('cfgCloudCode',    _cfgGet(c, 'cloud.activation_code',   ''));
  setVal('cfgCloudAutoRun', _cfgGet(c, 'cloud.auto_run_plans',    false));
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
  const _dAddr = txt('cfgAlpacaDefaultAddr').trim();
  const _dPort = num('cfgAlpacaDefaultPort', true);
  set('alpaca.default_server', _dAddr ? { address: _dAddr, port: _dPort || 11111 } : null);
  const _obsName = txt('cfgObsLocationName').trim();
  if (_obsName) set('observatory.name', _obsName);
  const _lat = num('cfgObsLat'), _lon = num('cfgObsLon');
  set('safety.observer.latitude',  _lat);
  set('safety.observer.longitude', _lon);
  set('observatory.latitude',  _lat);
  set('observatory.longitude', _lon);
  set('devices.telescope.enabled',         chk('cfgDevTelEnabled'));
  set('devices.telescope.device_number',   num('cfgDevTelNum', true));
  set('devices.camera.enabled',            chk('cfgDevCamEnabled'));
  set('devices.camera.device_number',      num('cfgDevCamNum', true));
  set('devices.focuser.enabled',           chk('cfgDevFocEnabled'));
  set('devices.focuser.device_number',     num('cfgDevFocNum', true));
  set('devices.filterwheel.enabled',           chk('cfgDevFwEnabled'));
  set('devices.filterwheel.device_number',     num('cfgDevFwNum', true));
  set('devices.covercalibrator.enabled',       chk('cfgDevCoverEnabled'));
  set('devices.covercalibrator.device_number', num('cfgDevCoverNum', true));
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
  set('cloud.enabled',          chk('cfgCloudEnabled'));
  set('cloud.url',              txt('cfgCloudUrl').trim());
  const _code = txt('cfgCloudCode').trim().toUpperCase();
  if (_code) set('cloud.activation_code', _code);
  set('cloud.auto_run_plans',   chk('cfgCloudAutoRun'));
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

async function obsLookupLocation() {
  const q = (document.getElementById('cfgObsLocationName')?.value || '').trim();
  const statusEl = document.getElementById('cfgObsLookupStatus');
  if (!q) { if (statusEl) statusEl.textContent = 'Enter a location name first.'; return; }
  if (statusEl) statusEl.textContent = 'Looking up…';
  try {
    const r = await fetch('/api/geocode?q=' + encodeURIComponent(q));
    const d = await r.json();
    if (!r.ok || d.error) {
      if (statusEl) statusEl.textContent = d.error || 'Not found.';
      return;
    }
    document.getElementById('cfgObsLat').value = d.latitude;
    document.getElementById('cfgObsLon').value = d.longitude;
    if (statusEl) statusEl.textContent = '✓ ' + d.display_name.split(',').slice(0,3).join(',');
  } catch(e) {
    if (statusEl) statusEl.textContent = 'Lookup failed: ' + e.message;
  }
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

// ── Night Schedule ────────────────────────────────────────────────────────────

let _schedule    = [];
let _schedEditId = null;
let _schedColorN = 0;
let _schedDragId = null;
let _schedCat    = null;
let _schedCatIdx = -1;

const _SC = ['sc0','sc1','sc2','sc3','sc4','sc5','sc6','sc7'];

let _schedTickInterval = null;

function openScheduleModal() {
  document.getElementById("schedModal").classList.remove("hidden");
  schedLoadCat();
  schedRebuild();
  if (!_schedTickInterval) {
    _schedTickInterval = setInterval(schedTickNow, 60000);
  }
  schedTickNow();
  schedFetchSiteAndSky();
}

function closeScheduleModal() {
  schedCloseAdd();
  document.getElementById("schedModal").classList.add("hidden");
}

// ── Time helpers ─────────────────────────────────────────────────────────────

function schedNight() {
  const parseT = id => {
    const v = document.getElementById(id).value;
    const [h,m] = v.split(":").map(Number);
    return h * 60 + m;
  };
  const s = parseT("schedNightStart");
  let e   = parseT("schedNightEnd");
  if (e <= s) e += 1440;
  return { s, e, len: e - s };
}

function schedT2M(t) {
  const [h, m] = t.split(":").map(Number);
  return h * 60 + m;
}

function schedM2T(m) {
  const total = ((m % 1440) + 1440) % 1440;
  return `${String(Math.floor(total/60)).padStart(2,"0")}:${String(total%60).padStart(2,"0")}`;
}

function schedDur(item) {
  // minutes, including 2-min slew/settle overhead
  return Math.max(1, Math.ceil(item.expDur * item.expCount / 60) + 2);
}

// ── Conflict detection ───────────────────────────────────────────────────────

function schedConflicts() {
  const { s } = schedNight();
  _schedule.forEach(a => { a._conflict = false; });
  for (let i = 0; i < _schedule.length; i++) {
    let as = schedT2M(_schedule[i].startTime);
    if (as < s - 120) as += 1440;
    const ae = as + schedDur(_schedule[i]);
    for (let j = i+1; j < _schedule.length; j++) {
      let bs = schedT2M(_schedule[j].startTime);
      if (bs < s - 120) bs += 1440;
      const be = bs + schedDur(_schedule[j]);
      if (as < be && ae > bs) {
        _schedule[i]._conflict = true;
        _schedule[j]._conflict = true;
      }
    }
  }
}

// ── Main rebuild ─────────────────────────────────────────────────────────────

function schedRebuild() {
  schedConflicts();
  const sorted = [..._schedule].sort((a,b) => {
    const { s } = schedNight();
    let am = schedT2M(a.startTime); if (am < s - 120) am += 1440;
    let bm = schedT2M(b.startTime); if (bm < s - 120) bm += 1440;
    return am - bm;
  });
  schedDrawTimeline();
  schedDrawList(sorted);
  schedDrawStats();
}

// ── Timeline ─────────────────────────────────────────────────────────────────

function schedTickNow() {
  const { s, len } = schedNight();
  const now = new Date();
  const nowM = now.getHours()*60 + now.getMinutes();
  let rel = nowM - s;
  if (rel < 0) rel += 1440;
  const el = document.getElementById("schedTlNow");
  if (el && rel >= 0 && rel <= len) {
    el.style.display = "block";
    el.style.left = (rel / len * 100) + "%";
  } else if (el) {
    el.style.display = "none";
  }
}

function schedDrawTimeline() {
  const { s, e, len } = schedNight();
  const labels = document.getElementById("schedTlLabels");
  const track  = document.getElementById("schedTlTrack");

  // Labels every 2 h
  labels.innerHTML = "";
  for (let m = 0; m <= len; m += 120) {
    const span = document.createElement("span");
    span.textContent = schedM2T(s + m);
    span.style.cssText = "position:absolute;";
    const pct = m / len * 100;
    span.style.left = pct + "%";
    if (pct > 90)      span.style.transform = "translateX(-100%)";
    else if (pct > 5)  span.style.transform = "translateX(-50%)";
    labels.appendChild(span);
  }

  // Remove old blocks (keep #schedTlNow)
  Array.from(track.children).forEach(el => {
    if (el.id !== "schedTlNow") el.remove();
  });

  _schedule.forEach(item => {
    let ms = schedT2M(item.startTime);
    if (ms < s - 120) ms += 1440;
    const dur  = schedDur(item);
    const left = Math.max(0, (ms - s) / len * 100);
    const w    = Math.min(100 - left, dur / len * 100);
    if (w <= 0) return;
    const block = document.createElement("div");
    block.className = `sched-tl-block ${item.color}${item._conflict ? " conflict" : ""}`;
    block.style.left  = left + "%";
    block.style.width = Math.max(w, 0.4) + "%";
    block.textContent = item.target.split(" –")[0];
    block.title = `${item.target}  ·  ${item.startTime}  ·  ${dur} min`;
    block.onclick = () => schedOpenAdd(item.id);
    track.insertBefore(block, document.getElementById("schedTlNow"));
  });

  schedTickNow();
}

// ── List ─────────────────────────────────────────────────────────────────────

function schedDrawList(sorted) {
  const body  = document.getElementById("schedBody");
  const empty = document.getElementById("schedEmpty");

  // Remove existing item divs
  Array.from(body.querySelectorAll(".sched-item")).forEach(el => el.remove());

  empty.style.display = _schedule.length === 0 ? "flex" : "none";

  sorted.forEach((item, idx) => {
    const dur = schedDur(item);
    const el  = document.createElement("div");
    el.className = `sched-item${item._conflict ? " conflict" : ""}`;
    el.dataset.id = item.id;
    el.draggable  = true;
    el.innerHTML  = `
      <div class="sched-drag" title="Drag to reorder">⠿</div>
      <div class="sched-item-num">${idx+1}</div>
      <div class="sched-item-main">
        <div class="sched-item-name">
          <span style="color:var(--green-hi);">${item.target}</span>
          ${item.objType ? `<span class="sched-type-badge">${item.objType}</span>` : ""}
          ${item._conflict ? `<span class="sched-type-badge" style="border-color:var(--red);color:var(--red);">Overlap</span>` : ""}
        </div>
        <div class="sched-item-coords">RA ${fmtRA(item.ra)} · Dec ${fmtDec(item.dec)}</div>
        <div class="sched-item-exp">
          <span>${item.expDur}s × ${item.expCount} frames</span>
          <span style="color:var(--dim);">· bin ${item.binning}×</span>
          <span style="color:var(--dim);">· ~${dur} min</span>
        </div>
        ${item.note ? `<div class="sched-item-note">${escH(item.note)}</div>` : ""}
      </div>
      <div class="sched-item-time">
        <input class="sched-time-inp" type="time" value="${item.startTime}"
          onchange="schedSetTime('${item.id}',this.value)" title="Start time">
        <div class="sched-dur">end <span>${schedM2T(schedT2M(item.startTime)+dur)}</span></div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0;">
        <canvas class="sched-sparkline" width="88" height="28"></canvas>
        <div class="sched-item-btns">
          <button class="btn btn-dim" onclick="schedOpenAdd('${item.id}')"
            style="font-size:10px;padding:3px 8px;">Edit</button>
          <button class="btn btn-red"  onclick="schedDel('${item.id}')"
            style="font-size:10px;padding:3px 8px;">✕</button>
        </div>
      </div>`;

    // Draw altitude sparkline
    const sparkCanvas = el.querySelector(".sched-sparkline");
    if (sparkCanvas && item.ra != null && item.dec != null) {
      const ns = document.getElementById("schedNightStart").value || "20:00";
      const ne = document.getElementById("schedNightEnd").value   || "05:00";
      const [nsh, nsm] = ns.split(":").map(Number);
      const [neh, nem] = ne.split(":").map(Number);
      drawAltSparkline(sparkCanvas, item.ra, item.dec, nsh + nsm/60, neh + nem/60);
    }

    el.addEventListener("dragstart", ev => {
      _schedDragId = item.id;
      el.classList.add("dragging");
      ev.dataTransfer.effectAllowed = "move";
    });
    el.addEventListener("dragend", () => {
      _schedDragId = null;
      el.classList.remove("dragging");
      document.querySelectorAll(".sched-item.drag-over").forEach(x => x.classList.remove("drag-over"));
    });
    el.addEventListener("dragover", ev => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      el.classList.add("drag-over");
    });
    el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
    el.addEventListener("drop", ev => {
      ev.preventDefault();
      el.classList.remove("drag-over");
      if (!_schedDragId || _schedDragId === item.id) return;
      const src = _schedule.find(x => x.id === _schedDragId);
      const dst = item;
      if (!src || !dst) return;
      // Swap start times to effectively reorder
      [src.startTime, dst.startTime] = [dst.startTime, src.startTime];
      schedRebuild();
    });

    // Insert before the add-window if it exists
    const addWin = document.getElementById("schedAddWindow");
    if (addWin) body.insertBefore(el, addWin);
    else body.appendChild(el);
  });
}

function schedDrawStats() {
  const el = document.getElementById("schedStats");
  const { len } = schedNight();
  const used  = _schedule.reduce((a, x) => a + schedDur(x), 0);
  const pct   = len > 0 ? Math.min(100, Math.round(used / len * 100)) : 0;
  const cfl   = _schedule.filter(x => x._conflict).length;
  if (_schedule.length === 0) { el.textContent = "No observations planned"; return; }
  el.innerHTML = `<span>${_schedule.length}</span> obs · <span>${used}</span> min · <span>${pct}%</span> of night${cfl ? ` · <span style="color:var(--red)">${cfl} conflict${cfl>1?"s":""}</span>` : ""}`;
}

function escH(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function schedSetTime(id, val) {
  const item = _schedule.find(x => x.id === id);
  if (item) { item.startTime = val; schedRebuild(); }
}

function schedDel(id) {
  _schedule = _schedule.filter(x => x.id !== id);
  schedRebuild();
}

function schedClearAll() {
  if (_schedule.length === 0) return;
  if (!confirm("Clear all scheduled observations?")) return;
  _schedule = [];
  schedRebuild();
}

// ── Add / Edit inner window ───────────────────────────────────────────────────

function schedOpenAdd(editId) {
  _schedEditId = editId;
  const ex = editId ? _schedule.find(x => x.id === editId) : null;

  // Compute default start time (end of last scheduled item)
  let defStart = document.getElementById("schedNightStart").value;
  if (!ex && _schedule.length > 0) {
    const { s } = schedNight();
    const srt = [..._schedule].sort((a,b) => {
      let am = schedT2M(a.startTime); if (am < s-120) am += 1440;
      let bm = schedT2M(b.startTime); if (bm < s-120) bm += 1440;
      return am - bm;
    });
    const last = srt[srt.length-1];
    let lm = schedT2M(last.startTime); if (lm < s-120) lm += 1440;
    defStart = schedM2T(lm + schedDur(last));
  }

  const old = document.getElementById("schedAddWindow");
  if (old) old.remove();

  const win = document.createElement("div");
  win.className = "sched-add-window";
  win.id = "schedAddWindow";

  const hintMsg = ex
    ? "Edit target, timing, or exposure settings, then save."
    : (_schedule.length > 0
        ? `${_schedule.length} observation${_schedule.length>1?"s":"" } already planned — suggested start time auto-filled.`
        : "Search the object catalog, or type RA/Dec directly.");

  // Compute altitude pill for edit mode
  let altPill = "";
  if (ex && _schedSiteLat !== null) {
    const obsDate = schedMinToDate(schedT2M(ex.startTime));
    const alt = computeAltitude(ex.ra, ex.dec, _schedSiteLat, _schedSiteLon, obsDate);
    const cls = alt >= 40 ? "good" : alt >= 20 ? "ok" : "bad";
    altPill = `<span class="sched-alt-pill ${cls}">▲ ${alt.toFixed(1)}° at start</span>`;
  }

  win.innerHTML = `
    <div class="sched-add-card">
      <div class="sched-add-hdr">
        <div style="display:flex;align-items:center;gap:10px;">
          <span>${ex ? "✏ Edit Observation" : "➕ Add Observation"}</span>
          ${altPill}
        </div>
        <button class="modal-close" onclick="schedCloseAdd()">×</button>
      </div>

      <!-- Hint -->
      <div class="sched-add-section" style="padding-top:12px;padding-bottom:12px;">
        <div class="sched-hint"><strong>Adaptive scheduler</strong> — ${escH(hintMsg)}</div>
      </div>

      <!-- Target -->
      <div class="sched-add-section">
        <div class="sched-add-section-lbl">Target</div>
        <div style="position:relative;">
          <input class="inp" id="schedTgtInp" type="text"
            placeholder="Search M42, Andromeda, nebula, globular…"
            autocomplete="off" spellcheck="false"
            value="${ex ? escH(ex.target) : ""}"
            oninput="schedCatFilter();schedUpdateAltPill()"
            onfocus="schedCatFilter()"
            onkeydown="schedCatKeyNav(event)">
          <div id="schedCatDrop"
            style="display:none;position:fixed;z-index:9999;background:#161b22;
              border:1px solid var(--border);border-radius:6px;
              max-height:220px;overflow-y:auto;box-shadow:0 8px 28px rgba(0,0,0,0.8);"></div>
        </div>
        <div class="inp-grid">
          <div class="inp-group">
            <div class="inp-label">R.A. (decimal hours, 0–24)</div>
            <input class="inp" id="schedRAInp" type="number"
              min="0" max="23.9999" step="0.0001" placeholder="0.0000"
              value="${ex ? ex.ra : ""}" oninput="schedUpdateAltPill()">
          </div>
          <div class="inp-group">
            <div class="inp-label">Dec (decimal degrees, ±90)</div>
            <input class="inp" id="schedDecInp" type="number"
              min="-90" max="90" step="0.0001" placeholder="0.0000"
              value="${ex ? ex.dec : ""}" oninput="schedUpdateAltPill()">
          </div>
        </div>
        <div id="schedAltRow" style="display:flex;align-items:center;gap:10px;min-height:22px;"></div>
      </div>

      <!-- Timing -->
      <div class="sched-add-section">
        <div class="sched-add-section-lbl">Timing</div>
        <div style="display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap;">
          <div class="inp-group" style="flex:0 0 auto;">
            <div class="inp-label">Start Time</div>
            <input class="inp" id="schedStartInp" type="time"
              value="${ex ? ex.startTime : defStart}"
              style="font-size:15px;padding:6px 12px;color:var(--green-hi);font-weight:bold;width:auto;"
              oninput="schedUpdateAltPill()">
          </div>
          <div id="schedEstEl" style="font-size:11px;color:var(--dim);padding-bottom:8px;line-height:1.7;"></div>
        </div>
      </div>

      <!-- Exposures -->
      <div class="sched-add-section">
        <div class="sched-add-section-lbl">Exposure Settings</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;">
          <div class="inp-group">
            <div class="inp-label">Duration (s)</div>
            <input class="inp" id="schedExpDurInp" type="number"
              min="0.1" step="0.1" placeholder="60"
              value="${ex ? ex.expDur : (_schedSkyData && _schedSkyData.cloudCover > 60 ? 120 : 60)}"
              oninput="schedUpdateEstimate()">
          </div>
          <div class="inp-group">
            <div class="inp-label">Frame Count</div>
            <input class="inp" id="schedExpCntInp" type="number"
              min="1" step="1" placeholder="10"
              value="${ex ? ex.expCount : 10}"
              oninput="schedUpdateEstimate()">
          </div>
          <div class="inp-group">
            <div class="inp-label">Binning</div>
            <input class="inp" id="schedBinInp" type="number"
              min="1" max="8" step="1" placeholder="1"
              value="${ex ? ex.binning : 1}">
          </div>
        </div>
      </div>

      <!-- Note -->
      <div class="sched-add-section" style="border-bottom:none;">
        <div class="sched-add-section-lbl">Note (optional)</div>
        <input class="inp" id="schedNoteInp" type="text"
          placeholder="e.g. Use Hα filter, guide on HD 1234…"
          value="${ex ? escH(ex.note || "") : ""}">
      </div>

      <div class="sched-add-foot">
        <button class="btn btn-dim" onclick="schedCloseAdd()">Cancel</button>
        <button class="btn btn-green" onclick="schedSave()">${ex ? "Save Changes" : "Add to Schedule"}</button>
      </div>
    </div>`;

  // Attach to body so it's not clipped by any scrolling parent
  document.body.appendChild(win);

  setTimeout(() => {
    const inp = document.getElementById("schedTgtInp");
    if (inp) { inp.focus(); if (!ex) inp.select(); }
    schedUpdateEstimate();
    schedUpdateAltPill();
  }, 30);
}

function schedCloseAdd() {
  const w = document.getElementById("schedAddWindow");
  if (w) w.remove();
  const d = document.getElementById("schedCatDrop");
  if (d) d.style.display = "none";
  _schedEditId = null;
}

function schedUpdateEstimate() {
  const dur = parseFloat(document.getElementById("schedExpDurInp")?.value || 0);
  const cnt = parseInt(document.getElementById("schedExpCntInp")?.value || 0);
  const el  = document.getElementById("schedEstEl");
  if (!el) return;
  if (!isNaN(dur) && !isNaN(cnt) && dur > 0 && cnt > 0) {
    const total = Math.ceil(dur * cnt / 60) + 2;
    el.innerHTML = `Estimated block: <strong style="color:var(--text)">${total} min</strong> (${dur}s × ${cnt} = ${Math.ceil(dur*cnt/60)} min data + 2 min overhead)`;
  } else {
    el.textContent = "";
  }
}

function schedSave() {
  const target   = document.getElementById("schedTgtInp")?.value?.trim();
  const ra       = parseFloat(document.getElementById("schedRAInp")?.value);
  const dec      = parseFloat(document.getElementById("schedDecInp")?.value);
  const startTime = document.getElementById("schedStartInp")?.value;
  const expDur   = parseFloat(document.getElementById("schedExpDurInp")?.value);
  const expCount = parseInt(document.getElementById("schedExpCntInp")?.value);
  const binning  = parseInt(document.getElementById("schedBinInp")?.value) || 1;
  const note     = document.getElementById("schedNoteInp")?.value?.trim();

  if (!target)              { alert("Please enter a target name."); return; }
  if (isNaN(ra)||isNaN(dec)){ alert("Please enter valid RA / Dec coordinates."); return; }
  if (!startTime)           { alert("Please set a start time."); return; }
  if (isNaN(expDur)||expDur<=0) { alert("Exposure duration must be > 0."); return; }
  if (isNaN(expCount)||expCount<1) { alert("Exposure count must be ≥ 1."); return; }

  if (_schedEditId) {
    const item = _schedule.find(x => x.id === _schedEditId);
    if (item) Object.assign(item, { target, ra, dec, startTime, expDur, expCount, binning, note });
  } else {
    _schedule.push({
      id: "sc" + Date.now(),
      target, ra, dec, startTime, expDur, expCount, binning, note,
      objType: "",
      color: _SC[_schedColorN++ % _SC.length],
      _conflict: false,
    });
  }

  schedCloseAdd();
  schedRebuild();
}

// ── Catalog search for schedule ───────────────────────────────────────────────

async function schedLoadCat() {
  if (_schedCat) return;
  try {
    const r = await fetch("/api/catalog");
    _schedCat = await r.json();
  } catch {}
}

function schedCatFilter() {
  const inp  = document.getElementById("schedTgtInp");
  const drop = document.getElementById("schedCatDrop");
  if (!inp || !drop || !_schedCat) { if(drop) drop.style.display="none"; return; }
  const q = inp.value.trim().toLowerCase();
  if (!q) { drop.style.display = "none"; return; }

  const hits = _schedCat.filter(o =>
    o.id.toLowerCase().includes(q) ||
    (o.name && o.name.toLowerCase().includes(q)) ||
    (o.type && o.type.toLowerCase().includes(q))
  ).slice(0, 12);

  if (!hits.length) { drop.style.display = "none"; return; }

  const rect = inp.getBoundingClientRect();
  drop.style.top   = (rect.bottom + 4) + "px";
  drop.style.left  = rect.left + "px";
  drop.style.width = rect.width + "px";
  drop.style.display = "block";
  _schedCatIdx = -1;

  drop.innerHTML = "";
  hits.forEach((obj, i) => {
    const row = document.createElement("div");
    row.style.cssText = "padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;transition:background 0.1s;";
    row.innerHTML = `
      <div>
        <span style="color:var(--text);font-size:13px;">${escH(obj.id)}</span>
        ${obj.name ? `<span style="color:var(--dim);font-size:11px;margin-left:7px;">${escH(obj.name)}</span>` : ""}
      </div>
      <div style="text-align:right;flex-shrink:0;margin-left:10px;">
        <div style="font-size:9px;color:var(--gray);padding:1px 5px;border:1px solid var(--border);display:inline-block;">${escH(obj.type)}</div>
        <div style="font-size:10px;color:var(--dim);margin-top:2px;">${fmtRA(obj.ra)} / ${fmtDec(obj.dec)}</div>
      </div>`;
    row.addEventListener("mousedown", ev => { ev.preventDefault(); schedCatPick(obj); });
    row.addEventListener("mouseover", () => {
      _schedCatIdx = i;
      drop.querySelectorAll("div[style]").forEach((d,j) =>
        d.style.background = j===i ? "rgba(88,166,255,0.12)" : "");
    });
    drop.appendChild(row);
  });
}

function schedCatPick(obj) {
  const inp  = document.getElementById("schedTgtInp");
  const drop = document.getElementById("schedCatDrop");
  if (inp)  inp.value  = obj.id + (obj.name ? " – " + obj.name : "");
  if (drop) drop.style.display = "none";
  const raEl  = document.getElementById("schedRAInp");
  const decEl = document.getElementById("schedDecInp");
  if (raEl)  raEl.value  = obj.ra;
  if (decEl) decEl.value = obj.dec;

  // Store type on the pending item
  window._schedPendingType = obj.type;
}

function schedCatKeyNav(e) {
  const drop = document.getElementById("schedCatDrop");
  if (!drop || drop.style.display === "none") return;
  const rows = drop.querySelectorAll("div[style]");
  if (e.key === "ArrowDown") {
    e.preventDefault();
    _schedCatIdx = Math.min(_schedCatIdx+1, rows.length-1);
    rows.forEach((d,i) => d.style.background = i===_schedCatIdx ? "rgba(88,166,255,0.12)" : "");
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    _schedCatIdx = Math.max(_schedCatIdx-1, 0);
    rows.forEach((d,i) => d.style.background = i===_schedCatIdx ? "rgba(88,166,255,0.12)" : "");
  } else if (e.key === "Enter" && _schedCatIdx >= 0) {
    e.preventDefault();
    rows[_schedCatIdx]?.dispatchEvent(new MouseEvent("mousedown"));
  } else if (e.key === "Escape") {
    drop.style.display = "none";
  }
}

document.addEventListener("click", ev => {
  const drop = document.getElementById("schedCatDrop");
  const inp  = document.getElementById("schedTgtInp");
  if (drop && inp && !inp.contains(ev.target) && !drop.contains(ev.target))
    drop.style.display = "none";
});

// ── Auto-fill ─────────────────────────────────────────────────────────────────

// ── Astronomy helpers ─────────────────────────────────────────────────────────

function dateToJD(date) {
  return date.getTime() / 86400000 + 2440587.5;
}

function computeAltitude(ra_h, dec_deg, lat_deg, lon_deg, date) {
  if (lat_deg == null || lon_deg == null) return 45;
  const toRad = d => d * Math.PI / 180;
  const JD = dateToJD(date);
  const T  = (JD - 2451545.0) / 36525;
  let GMST = 280.46061837 + 360.98564736629 * (JD - 2451545.0) + 0.000387933 * T * T;
  GMST = ((GMST % 360) + 360) % 360;
  const LST = (GMST + lon_deg + 360) % 360;       // degrees
  const HA  = (LST - ra_h * 15 + 360) % 360;      // degrees
  const lat = toRad(lat_deg), dec = toRad(dec_deg), ha = toRad(HA);
  const sinAlt = Math.sin(lat)*Math.sin(dec) + Math.cos(lat)*Math.cos(dec)*Math.cos(ha);
  return Math.asin(Math.max(-1, Math.min(1, sinAlt))) * 180 / Math.PI;
}

function schedMinToDate(absMin) {
  const d = new Date(); d.setHours(0,0,0,0);
  return new Date(d.getTime() + absMin * 60000);
}

// ── Site + sky data ───────────────────────────────────────────────────────────

let _schedSiteLat = null;
let _schedSiteLon = null;
let _schedSkyData = null;

async function schedFetchSiteAndSky() {
  try {
    const r = await fetch("/api/config/parsed");
    if (!r.ok) return;
    const cfg = await r.json();
    const obs = cfg?.safety?.observer || {};
    const lat = parseFloat(obs.latitude  || obs.lat || 0);
    const lon = parseFloat(obs.longitude || obs.lon || 0);
    if (isNaN(lat) || isNaN(lon) || (lat === 0 && lon === 0)) return;
    _schedSiteLat = lat;
    _schedSiteLon = lon;
    await Promise.all([
      schedFetchTwilight(lat, lon),
      schedFetchSky(lat, lon),
    ]);
  } catch (err) { console.warn("schedFetchSiteAndSky:", err); }
}

async function schedFetchTwilight(lat, lon) {
  try {
    const fmtDate = d => d.toISOString().split("T")[0];
    const today    = new Date();
    const tomorrow = new Date(today); tomorrow.setDate(tomorrow.getDate() + 1);
    const [r1, r2] = await Promise.all([
      fetch(`https://api.sunrise-sunset.org/json?lat=${lat}&lng=${lon}&date=${fmtDate(today)}&formatted=0`),
      fetch(`https://api.sunrise-sunset.org/json?lat=${lat}&lng=${lon}&date=${fmtDate(tomorrow)}&formatted=0`),
    ]);
    if (!r1.ok || !r2.ok) return;
    const [d1, d2] = await Promise.all([r1.json(), r2.json()]);
    const fmtHM = dt => `${String(dt.getHours()).padStart(2,"0")}:${String(dt.getMinutes()).padStart(2,"0")}`;
    // Evening: when sun drops below -18° (astronomical twilight ends) → observing begins
    const nightStart = fmtHM(new Date(d1.results.astronomical_twilight_end));
    // Morning: tomorrow's astronomical twilight begin → observing ends
    const nightEnd   = fmtHM(new Date(d2.results.astronomical_twilight_begin));
    document.getElementById("schedNightStart").value = nightStart;
    document.getElementById("schedNightEnd").value   = nightEnd;
    schedRebuild();
    schedUpdateSkyBar({ twilight: true, nightStart, nightEnd });
  } catch (err) { console.warn("schedFetchTwilight:", err); }
}

async function schedFetchSky(lat, lon) {
  try {
    const r = await fetch(
      `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}` +
      `&hourly=cloudcover,visibility&timezone=auto&forecast_days=1`
    );
    if (!r.ok) return;
    const d = await r.json();
    const hr = new Date().getHours();
    // Average cloud cover over the next 10 hours (the observing window)
    const slice = d.hourly.cloudcover.slice(hr, hr + 10);
    const avg   = slice.length ? Math.round(slice.reduce((a,b)=>a+b,0)/slice.length) : null;
    const vis   = d.hourly.visibility ? d.hourly.visibility[hr] : null;
    _schedSkyData = { cloudCover: avg, visibility: vis };
    schedUpdateSkyBar(_schedSkyData);
  } catch (err) { console.warn("schedFetchSky:", err); }
}

function schedUpdateSkyBar(data) {
  const bar = document.getElementById("schedSkyBar");
  if (!bar) return;
  bar.style.display = "flex";
  bar.innerHTML = "";

  const add = (icon, label, val, color) => {
    const el = document.createElement("div");
    el.className = "sched-sky-attr";
    el.innerHTML = `<span>${icon}</span><span>${label}: </span><strong style="color:${color||"var(--text)"}">${val}</strong>`;
    bar.appendChild(el);
  };

  const cc = data.cloudCover;
  if (cc != null) {
    const color = cc < 20 ? "var(--green)" : cc < 50 ? "var(--yellow)" : "var(--red)";
    const label = cc < 20 ? "Clear" : cc < 50 ? "Partly cloudy" : cc < 80 ? "Cloudy" : "Overcast";
    add("☁", "Sky", `${label} (${cc}%)`, color);
  }
  if (data.visibility != null) {
    add("👁", "Vis", `${(data.visibility/1000).toFixed(0)} km`, "var(--text)");
  }
  if (data.nightStart) {
    add("🌙", "Astro night", `${data.nightStart} → ${data.nightEnd}`, "var(--blue)");
  }
  if (_schedSiteLat !== null) {
    add("📍", "Site", `${_schedSiteLat.toFixed(2)}°, ${_schedSiteLon.toFixed(2)}°`, "var(--dim)");
  }
  const src = document.createElement("span");
  src.style.cssText = "margin-left:auto;font-size:9px;color:var(--gray);";
  src.textContent = "open-meteo.com · sunrise-sunset.org";
  bar.appendChild(src);
}

// ── Altitude pill in add/edit form ────────────────────────────────────────────

function schedUpdateAltPill() {
  const ra  = parseFloat(document.getElementById("schedRAInp")?.value);
  const dec = parseFloat(document.getElementById("schedDecInp")?.value);
  const t   = document.getElementById("schedStartInp")?.value;
  const row = document.getElementById("schedAltRow");
  if (!row) return;
  row.innerHTML = "";
  if (isNaN(ra) || isNaN(dec) || !t || _schedSiteLat == null) return;
  const absMin = schedT2M(t);
  const date   = schedMinToDate(absMin);
  const alt    = computeAltitude(ra, dec, _schedSiteLat, _schedSiteLon, date);
  const cls    = alt >= 40 ? "good" : alt >= 20 ? "ok" : "bad";
  const label  = alt >= 40 ? "Good altitude" : alt >= 20 ? "Low but visible" : "Below recommended horizon";
  row.innerHTML = `<span class="sched-alt-pill ${cls}">▲ ${alt.toFixed(1)}° — ${label}</span>`;
}

// ── Adaptive auto-fill (entire night, altitude-scored) ────────────────────────

async function schedAutoFill() {
  if (!_schedCat || !_schedCat.length) {
    await schedLoadCat();
    if (!_schedCat || !_schedCat.length) { alert("Catalog not loaded yet."); return; }
  }

  const { s, e } = schedNight();
  const GOOD = new Set(["Galaxy","Nebula","Emission Nebula","Reflection Nebula",
    "Planetary Nebula","Open Cluster","Globular Cluster","Supernova Remnant",
    "HII Ionized region","Double star","Association of stars"]);

  // Adapt exposure defaults to sky conditions
  const cc = _schedSkyData?.cloudCover ?? 0;
  const expDur   = cc > 60 ? 120 : cc > 30 ? 90 : 60;
  const expCount = cc > 60 ? 5   : cc > 30 ? 7  : 10;

  // Find first free slot
  let next = s;
  if (_schedule.length > 0) {
    const srt = [..._schedule].sort((a,b) => {
      let am = schedT2M(a.startTime); if (am < s-120) am += 1440;
      let bm = schedT2M(b.startTime); if (bm < s-120) bm += 1440;
      return am - bm;
    });
    const last = srt[srt.length-1];
    let lm = schedT2M(last.startTime); if (lm < s-120) lm += 1440;
    next = lm + schedDur(last);
  }

  const usedIds = new Set(_schedule.map(x => x.target.split(" –")[0].trim()));
  const minSlot = Math.ceil(expDur * expCount / 60) + 2;
  let added = 0;

  while (next + minSlot <= e) {
    const pool = _schedCat.filter(o => GOOD.has(o.type) && !usedIds.has(o.id));
    if (!pool.length) break;

    const obsDate = schedMinToDate(next);

    // Score candidates by altitude at observation time
    // Sample ≤300 objects for performance; the catalog is sorted by Messier then NGC order
    // so earlier entries tend to be well-known / brighter objects
    const sample = pool.length > 300
      ? pool.slice(0, 150).concat(pool.slice(150).sort(() => Math.random()-0.5).slice(0, 150))
      : pool;

    let best = null, bestScore = -999;
    for (const obj of sample) {
      const alt = computeAltitude(obj.ra, obj.dec, _schedSiteLat, _schedSiteLon, obsDate);
      if (alt < 20) continue;                  // must clear horizon
      // Prefer altitudes 40–70° (avoids extreme airmass and polar-region issues)
      const altScore = alt > 75 ? 150 - alt : alt;
      if (altScore > bestScore) { bestScore = altScore; best = obj; }
    }

    if (!best) {
      // Nothing above 20° — try best available (may be low but schedulable)
      for (const obj of sample) {
        const alt = computeAltitude(obj.ra, obj.dec, _schedSiteLat, _schedSiteLon, obsDate);
        if (alt > bestScore) { bestScore = alt; best = obj; }
      }
    }
    if (!best) break;

    const noteCC = cc > 30 ? ` (${cc}% cloud)` : "";
    _schedule.push({
      id:        `sc${Date.now()}_${added}`,
      target:    best.id + (best.name ? " – " + best.name : ""),
      objType:   best.type,
      ra:        best.ra, dec: best.dec,
      startTime: schedM2T(next),
      expDur, expCount, binning: 1,
      note:      `Auto-filled${noteCC}`,
      color:     _SC[_schedColorN++ % _SC.length],
      _conflict: false,
    });
    usedIds.add(best.id);
    next += schedDur(_schedule[_schedule.length-1]);
    added++;
  }

  if (!added) alert("Night is already fully scheduled or no suitable objects found.");
  else schedRebuild();
}

// ── Run schedule ──────────────────────────────────────────────────────────────

let _schedStatusTimer = null;

async function schedRunAll() {
  if (_schedule.length === 0) { alert("No observations scheduled."); return; }
  const cfl = _schedule.filter(x => x._conflict).length;
  if (cfl && !confirm(`${cfl} observation${cfl>1?"s have":" has"} overlapping time slots. Run anyway?`)) return;

  const { s } = schedNight();
  const sorted = [..._schedule].sort((a,b) => {
    let am = schedT2M(a.startTime); if (am < s-120) am += 1440;
    let bm = schedT2M(b.startTime); if (bm < s-120) bm += 1440;
    return am - bm;
  });

  const r = await fetch("/api/schedule/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: sorted }),
  });
  const data = await r.json();
  if (!r.ok) { alert("Failed to start schedule: " + (data.error || r.statusText)); return; }

  document.getElementById("btnSchedRun").textContent = "Running…";
  document.getElementById("btnSchedRun").disabled = true;
  schedStartStatusPoll();
}

async function schedAbort() {
  await fetch("/api/schedule/abort", { method: "DELETE" });
}

function schedStartStatusPoll() {
  if (_schedStatusTimer) clearInterval(_schedStatusTimer);
  _schedStatusTimer = setInterval(schedPollStatus, 1500);
  schedPollStatus();
}

async function schedPollStatus() {
  try {
    const r = await fetch("/api/schedule/status");
    const st = await r.json();
    schedApplyStatus(st);
  } catch {}
}

function schedApplyStatus(st) {
  const liveEl    = document.getElementById("schedLive");
  const runBtn    = document.getElementById("btnSchedRun");
  const targetEl  = document.getElementById("schedLiveTarget");
  const phaseEl   = document.getElementById("schedLivePhase");
  const framesEl  = document.getElementById("schedLiveFrames");
  const progressEl= document.getElementById("schedLiveProgress");
  const barEl     = document.getElementById("schedLiveBar");

  if (st.running) {
    liveEl.style.display = "flex";
    targetEl.textContent  = st.current_target || "—";
    phaseEl.textContent   = st.current_phase  || "";

    if (st.current_phase === "exposing" && st.total_frames > 0) {
      framesEl.textContent = `Frame ${st.current_frame}/${st.total_frames}`;
      barEl.style.width = (st.current_frame / st.total_frames * 100) + "%";
    } else if (st.current_phase === "slewing") {
      framesEl.textContent = "Slewing…";
      barEl.style.width = "0%";
    } else if (st.current_phase === "waiting") {
      framesEl.textContent = "Waiting for start time…";
      barEl.style.width = "0%";
    }

    if (st.total > 0) {
      progressEl.textContent = `Obs ${st.completed + 1}/${st.total}`;
    }
    runBtn.textContent = "Running…";
    runBtn.disabled = true;

    // Highlight active / done items in the list
    document.querySelectorAll(".sched-item").forEach(el => {
      const id = el.dataset.id;
      const item = _schedule.find(x => x.id === id);
      if (!item) return;
      const { s } = schedNight();
      const sorted = [..._schedule].sort((a,b) => {
        let am = schedT2M(a.startTime); if (am < s-120) am += 1440;
        let bm = schedT2M(b.startTime); if (bm < s-120) bm += 1440;
        return am - bm;
      });
      const itemIdx = sorted.findIndex(x => x.id === id);
      el.classList.toggle("done-item",   itemIdx < st.completed);
      el.classList.toggle("active-item", itemIdx === st.current_idx);
    });

  } else {
    liveEl.style.display = "none";
    runBtn.textContent = "▶ Run Schedule";
    runBtn.disabled = false;
    document.querySelectorAll(".sched-item.done-item").forEach(el => el.classList.remove("done-item","active-item"));
    if (_schedStatusTimer) { clearInterval(_schedStatusTimer); _schedStatusTimer = null; }

    if (st.current_phase === "done") {
      histRefresh();
    }
    if (st.error) {
      console.error("Schedule error:", st.error);
    }
  }
}

// ── Camera History Modal ───────────────────────────────────────────────────────

let _histImages  = [];
let _histTimer   = null;
let _histPrevCnt = 0;

function openHistoryModal() {
  document.getElementById("histModal").classList.remove("hidden");
  histRefresh();
  _histTimer = setInterval(histRefresh, 4000);
}

function closeHistoryModal() {
  document.getElementById("histModal").classList.add("hidden");
  if (_histTimer) { clearInterval(_histTimer); _histTimer = null; }
}

async function histRefresh() {
  try {
    const r = await fetch("/api/history");
    const d = await r.json();
    _histImages = d.images || [];
    histRender();
    // Update badge
    if (_histImages.length > _histPrevCnt) {
      const badge = document.getElementById("histBadge");
      if (badge) {
        badge.style.display = "flex";
        badge.textContent = _histImages.length;
      }
    }
    _histPrevCnt = _histImages.length;
  } catch {}
}

function histFilter() {
  histRender();
}

function histRender() {
  const body    = document.getElementById("histBody");
  const empty   = document.getElementById("histEmpty");
  const countEl = document.getElementById("histCount");
  const dlBtn   = document.getElementById("btnHistDlAll");
  const q       = (document.getElementById("histSearch")?.value || "").toLowerCase();

  // Remove existing cards
  Array.from(body.querySelectorAll(".hist-card")).forEach(el => el.remove());

  const filtered = q
    ? _histImages.filter(img => img.target.toLowerCase().includes(q))
    : _histImages;

  empty.style.display = filtered.length === 0 ? "flex" : "none";
  countEl.innerHTML   = `<span>${filtered.length}</span> image${filtered.length!==1?"s":""} captured`;
  dlBtn.disabled      = filtered.length === 0;

  filtered.forEach(img => {
    const card = document.createElement("div");
    card.className = "hist-card";
    card.title     = img.target;
    card.innerHTML = `
      <div class="hist-thumb">
        <img src="data:image/png;base64,${img.thumb}" alt="${escH(img.target)}" loading="lazy">
      </div>
      <div class="hist-card-body">
        <div class="hist-card-name">${escH(img.target)}</div>
        <div class="hist-card-meta">${img.date} · ${img.ts}</div>
        <div class="hist-card-exp">${img.exp_dur}s · bin${img.binning}× · ${img.frame}/${img.total}</div>
      </div>`;
    card.addEventListener("click", () => openLightbox(img));
    body.insertBefore(card, empty);
  });
}

let _lbCurrentImg = null;

function _lbRenderMeta(img) {
  document.getElementById("histLbMeta").innerHTML =
    `<strong>${escH(img.target)}</strong><br>${img.date} · ${img.ts} &nbsp;·&nbsp; ${img.exp_dur}s exposure · binning ${img.binning}× · frame ${img.frame}/${img.total}`;
}

function openLightbox(img) {
  _lbCurrentImg = img;
  const lbImg = document.getElementById("histLbImg");
  const lbDl  = document.getElementById("histLbDl");

  lbImg.src  = `/api/history/${img.id}`;
  lbImg.alt  = img.target;
  _lbRenderMeta(img);
  lbDl.href = `/api/history/${img.id}`;
  lbDl.download = `${img.target.replace(/[^a-z0-9]/gi,"_")}_${img.ts.replace(/:/g,"")}.png`;

  document.getElementById("histLbEdit").classList.add("hidden");
  document.getElementById("histLightbox").classList.remove("hidden");
}

function closeLightbox() {
  document.getElementById("histLightbox").classList.add("hidden");
  document.getElementById("histLbImg").src = "";
  document.getElementById("histLbEdit").classList.add("hidden");
  _lbCurrentImg = null;
}

function openMetaEdit() {
  if (!_lbCurrentImg) return;
  const img = _lbCurrentImg;
  document.getElementById("metaTarget").value  = img.target;
  document.getElementById("metaExpDur").value  = img.exp_dur;
  document.getElementById("metaBinning").value = img.binning;
  document.getElementById("metaFrame").value   = img.frame;
  document.getElementById("metaTotal").value   = img.total;
  document.getElementById("histLbEdit").classList.remove("hidden");
}

function cancelMetaEdit() {
  document.getElementById("histLbEdit").classList.add("hidden");
}

async function saveMetaEdit() {
  if (!_lbCurrentImg) return;
  const payload = {
    target:  document.getElementById("metaTarget").value.trim(),
    exp_dur: parseFloat(document.getElementById("metaExpDur").value),
    binning: parseInt(document.getElementById("metaBinning").value, 10),
    frame:   parseInt(document.getElementById("metaFrame").value, 10),
    total:   parseInt(document.getElementById("metaTotal").value, 10),
  };
  try {
    const r = await fetch(`/api/history/${_lbCurrentImg.id}/metadata`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(await r.text());
    const {entry} = await r.json();
    Object.assign(_lbCurrentImg, entry);
    const idx = _histImages.findIndex(i => i.id === entry.id);
    if (idx >= 0) Object.assign(_histImages[idx], entry);
    _lbRenderMeta(_lbCurrentImg);
    document.getElementById("histLbEdit").classList.add("hidden");
    histRender();
  } catch(e) {
    alert("Failed to save metadata: " + e.message);
  }
}

async function histClearAll() {
  if (!_histImages.length) return;
  if (!confirm(`Delete all ${_histImages.length} images from history?`)) return;
  _histImages = [];
  histRender();
}

function histDownloadAll() {
  // Download each visible image sequentially
  const q = (document.getElementById("histSearch")?.value || "").toLowerCase();
  const list = q ? _histImages.filter(x => x.target.toLowerCase().includes(q)) : _histImages;
  list.forEach((img, i) => {
    setTimeout(() => {
      const a = document.createElement("a");
      a.href = `/api/history/${img.id}`;
      a.download = `${img.target.replace(/[^a-z0-9]/gi,"_")}_${img.ts.replace(/:/g,"")}.png`;
      a.click();
    }, i * 350);
  });
}

// Update history badge count periodically even when modal is closed
setInterval(async () => {
  try {
    const r = await fetch("/api/history");
    const d = await r.json();
    const cnt = (d.images || []).length;
    const badge = document.getElementById("histBadge");
    if (badge && cnt > 0) {
      badge.style.display = "flex";
      badge.textContent   = cnt;
    }
  } catch {}
}, 8000);

// ── Resizable log divider ─────────────────────────────────────────────────────
(function() {
  const handle = document.getElementById("logResizer");
  const footer = document.querySelector(".log-footer");
  if (!handle || !footer) return;
  let startY, startH;
  handle.addEventListener("mousedown", e => {
    e.preventDefault();
    startY = e.clientY;
    startH = footer.getBoundingClientRect().height;
    handle.classList.add("dragging");
    document.body.style.cursor = "ns-resize";
    document.body.style.userSelect = "none";
    function onMove(e) {
      const delta = startY - e.clientY;
      footer.style.height = Math.max(40, Math.min(window.innerHeight * 0.75, startH + delta)) + "px";
    }
    function onUp() {
      handle.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
})();

// ── Resizable panel divider ───────────────────────────────────────────────────
(function() {
  const handle = document.getElementById("panelResizer");
  const row    = document.getElementById("panelRow");
  if (!handle || !row) return;
  let startX, startLeftW;
  handle.addEventListener("mousedown", e => {
    e.preventDefault();
    startX = e.clientX;
    const tel = document.getElementById("telModal");
    startLeftW = tel ? tel.getBoundingClientRect().width : row.clientWidth / 2;
    handle.classList.add("dragging");
    document.body.style.cursor = "ew-resize";
    document.body.style.userSelect = "none";
    function onMove(e) {
      const tel = document.getElementById("telModal");
      const cam = document.getElementById("camModal");
      if (!tel || !cam) return;
      const rowW = row.clientWidth;
      const hW   = handle.offsetWidth;
      const avail = rowW - hW;
      const delta = e.clientX - startX;
      const newLeft = Math.max(160, Math.min(avail - 160, startLeftW + delta));
      tel.style.flex = "0 0 " + newLeft + "px";
      cam.style.flex = "1 1 0";
    }
    function onUp() {
      handle.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
})();

// ── First-run activation modal ────────────────────────────────────────────────

function closeWelcomeModal() {
  document.getElementById('welcomeModal').classList.add('hidden');
}

function _isPlaceholderCode(code) {
  return !code || code === 'ACTIVATION_CODE_PLACEHOLDER' || code.trim() === '';
}

function openWelcomeModal() {
  document.getElementById('welcomeModal').classList.remove('hidden');
  setTimeout(() => document.getElementById('welcomeCodeInput').focus(), 80);
}

async function _checkFirstRun() {
  try {
    const [cfgR, cloudR] = await Promise.all([
      fetch('/api/config/parsed'),
      fetch('/api/cloud').catch(() => null),
    ]);
    if (!cfgR.ok) return;
    const cfg = await cfgR.json();
    const cloudCfg = cfg.cloud || {};
    const registered = cloudR && cloudR.ok && (await cloudR.json()).registered;
    const setupBanner = document.getElementById('setupBanner');
    if (registered) {
      if (setupBanner) setupBanner.style.display = 'none';
      return;
    }
    if (setupBanner) setupBanner.style.display = 'flex';
    // Pre-fill with any saved code so the user just has to click Connect
    const savedCode = (cloudCfg.activation_code || '').trim();
    if (savedCode && !_isPlaceholderCode(savedCode)) {
      const inp = document.getElementById('welcomeCodeInput');
      if (inp && !inp.value) inp.value = savedCode;
    }
    openWelcomeModal();
  } catch (_) {}
}

async function submitActivationCode() {
  const input  = document.getElementById('welcomeCodeInput');
  const errEl  = document.getElementById('welcomeErr');
  const btn    = document.getElementById('btnWelcomeConnect');
  const code   = input.value.trim().toUpperCase();
  errEl.textContent = '';
  if (!code) { errEl.textContent = 'Please enter your activation code.'; return; }
  if (!/^BS-\d{4}-[A-Z0-9]{8}$/.test(code)) {
    errEl.textContent = 'Code should look like BS-2024-XXXXXXXX.'; return;
  }
  btn.disabled = true; btn.textContent = 'Connecting…';
  try {
    const cfgR = await fetch('/api/config/parsed');
    const cfg  = cfgR.ok ? await cfgR.json() : {};
    if (!cfg.cloud) cfg.cloud = {};
    cfg.cloud.activation_code = code;
    cfg.cloud.enabled = true;
    if (!cfg.cloud.url) cfg.cloud.url = 'https://api.thetelescope.net';
    const r = await fetch('/api/config/parsed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      errEl.textContent = 'Save failed: ' + (d.error || r.statusText);
      btn.disabled = false; btn.textContent = 'Connect';
      return;
    }
    // Trigger immediate registration — no restart needed
    btn.textContent = 'Registering…';
    try {
      const reg = await fetch('/api/cloud/connect', { method: 'POST' });
      const rd  = await reg.json().catch(() => ({}));
      if (reg.ok && rd.ok) {
        const content = document.querySelector('#welcomeModal .modal-content');
        content.innerHTML = `
          <div style="font-size:56px;margin-bottom:12px;">✅</div>
          <div style="font-size:18px;font-weight:700;margin-bottom:8px;color:#fff;">Connected!</div>
          <div style="font-size:13px;color:var(--dim);line-height:1.6;">
            Your node is registered with The Telescope Net.
          </div>`;
        setTimeout(() => window.close(), 1800);
        return;
      }
      errEl.textContent = rd.error || 'Registration failed — check your code and try again.';
    } catch (_) {
      errEl.textContent = 'Could not reach cloud — check your internet connection.';
    }
    btn.disabled = false; btn.textContent = 'Connect';
  } catch (e) {
    errEl.textContent = 'Request failed: ' + e.message;
    btn.disabled = false; btn.textContent = 'Connect';
  }
}

document.addEventListener('DOMContentLoaded', _checkFirstRun);
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

def launch(port: int = 5173) -> None:
    global _safety_mgr, _image_watcher, _cloud

    import urllib.request
    import webbrowser

    cfg = _load_config()
    log_cfg = cfg.get("logging", {})
    logging.basicConfig(
        level=log_cfg.get("level", "INFO"),
        format=log_cfg.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    )
    logger.info("NODE v1 starting on port %d", port)

    # Prevent the host OS from sleeping during overnight observations
    try:
        from src.sleep_prevention import enable as _sleep_enable
        _sleep_enable()
    except Exception as exc:
        logger.warning("Sleep prevention unavailable: %s", exc)
    _load_history_from_disk()

    _safety_mgr = SafetyManager(config=cfg, on_unsafe=_on_safety_unsafe)
    _safety_mgr.start()

    iw_cfg = cfg.get("image_watcher", {})
    if iw_cfg.get("enabled", False):
        watch_path     = iw_cfg.get("watch_path", "/mnt/seestar")
        debounce_delay = float(iw_cfg.get("debounce_delay", 2.0))
        _image_watcher = ImageWatcher(watch_path, _on_new_fits, debounce_delay)
        _image_watcher.start()
        logger.info("Image watcher active: %s", watch_path)
        with _state_lock:
            _state["image_watcher"]["enabled"]    = True
            _state["image_watcher"]["watch_path"] = watch_path

    phot_cfg = cfg.get("photometry", {})
    if phot_cfg.get("enabled", False):
        with _state_lock:
            _state["photometry"]["enabled"] = True
        threading.Thread(target=_phot_worker, daemon=True, name="phot-worker").start()
        logger.info("Photometry pipeline enabled (node_id=%s)", phot_cfg.get("node_id", "?"))

    cloud_cfg = cfg.get("cloud", {})
    if cloud_cfg.get("enabled", False):
        _cloud = CloudCommunicator(
            cfg,
            get_conditions=_cloud_conditions,
            on_plan=_on_cloud_plan,
            get_telescope_specs=_cloud_telescope_specs,
        )
        _cloud.start()

    pc_cfg = cfg.get("pier_cam", {})
    if pc_cfg.get("enabled", False):
        _pier_cam_stop.clear()
        threading.Thread(target=_pier_cam_loop, daemon=True, name="pier-cam").start()
        with _state_lock:
            _state["pier_cam"]["enabled"] = True

    flask_thread = threading.Thread(
        target=lambda: app.run(
            host="127.0.0.1", port=port, debug=False,
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

    print(f"\n  NODE v1 running\n", file=sys.__stdout__)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down.", file=sys.__stdout__)
    finally:
        _pier_cam_stop.set()
        if _cloud is not None:
            _cloud.stop()
        if _safety_mgr is not None:
            _safety_mgr.stop()
        if _image_watcher is not None:
            _image_watcher.stop()
        try:
            from src.sleep_prevention import disable as _sleep_disable
            _sleep_disable()
        except Exception:
            pass


if __name__ == "__main__":
    launch()
