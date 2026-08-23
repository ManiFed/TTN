#!/usr/bin/env python3
"""
NODE v1 — background telescope agent (ALPACA control + cloud sync).

The desktop app (TelescopeNet.app) is the control surface; this exposes a
local JSON API on 127.0.0.1 for it to talk to.

Run:  python dashboard.py
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
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import yaml
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from pyongc.ongc import listObjects as _ongc_list
from alpaca.discovery import discover_servers
from alpaca.safety_manager import SafetyManager
from alpaca.telescope import Telescope
from alpaca.camera import Camera, ExposureCancelled
from alpaca.focuser import Focuser
from alpaca.autofocus import autofocus_device, AutofocusCancelled, AutofocusError
from alpaca.covercalibrator import CoverCalibrator
from src.image_watcher import ImageWatcher
from src.photometry import run_pipeline as _run_photometry
from src.photometry import run_survey_pipeline as _run_survey_pipeline
from src.aavso_submission import submit as _aavso_submit
from src.fits_export import export_enhanced_fits as _export_fits
from src.geolocation import enrich_config_with_location
from src.telescope_specs import enrich_config_with_telescope
from src.stacking import LiveStacker
from src.cloud_communicator import CloudCommunicator
from src import telemetry as _telemetry
from src.node_supervisor import NodeSupervisor
from src.commissioning import CommissioningManager


app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


@app.before_request
def _reject_cross_origin():
    """Block DNS-rebinding and browser CSRF against this local control API.

    The agent binds to 127.0.0.1, but a malicious web page can still reach it:
    via DNS rebinding (a hostname that resolves to 127.0.0.1 — caught by the
    Host check) or via a cross-origin form/fetch POST (the browser sends it
    with an Origin header — caught by the Origin check).  A native desktop app
    or curl sends neither a foreign Host nor an Origin, so it is unaffected.
    Commands here move a physical telescope; this must not be scriptable from
    a web page.
    """
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    if host and host not in {h.strip("[]") for h in _LOCAL_HOSTNAMES}:
        return jsonify({"error": "forbidden host"}), 403
    origin = request.headers.get("Origin", "")
    if origin:
        try:
            from urllib.parse import urlsplit
            o_host = (urlsplit(origin).hostname or "").lower()
        except ValueError:
            o_host = ""
        if o_host not in _LOCAL_HOSTNAMES:
            return jsonify({"error": "cross-origin request rejected"}), 403


# ── Shared state ───────────────────────────────────────────────────────────────

_state: dict[str, Any] = {
    "server":    None,
    "connected": False,
    "telescope": {
        "enabled":   False,
        "connected": False,
        "error":     None,       # user-facing reason the last connect attempt failed
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
        "error":            None,
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
        "error":     None,
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
        "history":     [],     # rolling last 20 measurements this session
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
        _telemetry.event("photometry_queue_full", severity="warning",
                         detail={"dropped": os.path.basename(fits_path),
                                 "queue_max": _PHOT_QUEUE_MAX})


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
    "source":          "",   # manual | cloud | interrupt
    "items":           [],   # full observation list for the current run
}

# ── Never-idle state: the plan's contingency alternates + starvation flag ──────
# Alternates are the cloud plan's contingency ladder — pre-valued fallback
# observations the node runs locally (no cloud round-trip) to fill schedule
# gaps, failed items, and leftover dark time after the plan is exhausted.
_alternates_lock = threading.Lock()
_alternates: list[dict] = []           # validated, ordered by expected_info
_alternates_used: set[int] = set()     # indexes into _alternates already run
# Set when plan + alternates are exhausted with dark time remaining; reported
# in the heartbeat detail so cloud reflow can top this node up. Cleared on any
# new plan or interrupt.
_work_starved = threading.Event()
# Snapshot of the un-run remainder of a cloud schedule cancelled by a safety
# trip, so the unsafe→safe transition can resume it without waiting for the
# next plan poll: {"items": [...], "at": monotonic}.
_resume_lock = threading.Lock()
_resume_after_safe: Optional[dict] = None
# Slew + settle margin assumed when deciding if an alternate fits a gap.
_GAP_FILL_MARGIN_S = 120.0

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
                   exp_dur: Optional[float] = None,
                   target: Optional[str] = None) -> Optional[str]:
    """Download the last camera image, store it globally, and return its b64.

    If fits_path is provided, also write a FITS file with the raw (un-stretched)
    pixel data and whatever header fields the camera exposes.  This is used by
    the schedule runner so the photometry pipeline has a science-grade file to
    work with rather than a display-stretched PNG.

    ``target``, when given, is written to the OBJECT header keyword — this is
    the only signal run_pipeline() and _frame_has_target() use to identify a
    targeted exposure vs. an anonymous survey/contributor frame, so a scheduled
    observation with no OBJECT silently falls through to the survey-only path
    and never produces a measurement.
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
                if target:
                    hdr["OBJECT"] = target
                # Pull what we can from the camera device
                if exp_dur is None:
                    with _state_lock:
                        exp_dur = _state["camera"].get("exposure_duration")
                # DATE-OBS is the exposure *start* per the FITS convention (the
                # photometry BJD mid-point correction depends on it); we save
                # right after readout, so start ≈ now − exposure duration.
                _start = _time.time() - float(exp_dur or 0.0)
                hdr["DATE-OBS"] = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime(_start))
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
                if ra is not None and dec is not None:
                    hdr["RA"]  = round(float(ra) * 15.0, 6)   # hours→degrees
                    hdr["DEC"] = round(float(dec), 6)
                # Durable execution provenance follows the frame into the
                # photometry and cloud payloads without changing camera APIs.
                with _sched_lock:
                    item_id = str(_sched_state.get("current_item_id") or "")
                    bundle_id = str(_sched_state.get("current_bundle_id") or "")
                    target = str(_sched_state.get("current_target") or "")
                    filt = str(_sched_state.get("current_filter") or "")
                if target:
                    hdr["OBJECT"] = target[:68]
                if filt:
                    hdr["FILTER"] = filt[:8]
                if item_id:
                    hdr["BSITEM"] = item_id[:68]
                if bundle_id:
                    hdr["BSBUNDLE"] = bundle_id[:68]
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
    if obj:
        parts.append(f"obj={obj}")
    if exptime:
        parts.append(f"exp={exptime}s")
    if filter_:
        parts.append(f"filter={filter_}")
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
    if _commissioning is not None:
        threading.Thread(
            target=_commissioning.observe_fits, args=(path,),
            daemon=True, name="commissioning-fits",
        ).start()

    # Optionally run photometry pipeline in background thread
    with _state_lock:
        phot_enabled = _state["photometry"]["enabled"]

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


def _frame_has_target(fits_path: str, cfg: dict) -> bool:
    """True when the frame (or config) names a target for differential
    photometry. Contributor-mode frames from NINA/ASIAIR capture dirs
    usually don't — they go down the full-frame survey path instead."""
    if str(cfg.get("photometry", {}).get("target", {}).get("name") or "").strip():
        return True
    try:
        from astropy.io import fits as _fits
        with _fits.open(fits_path, memmap=False,
                        ignore_missing_simple=True) as hdul:
            return bool(str(hdul[0].header.get("OBJECT", "")).strip())
    except Exception:
        return False


def _maybe_report_characterization() -> None:
    """Ship the rolling measured-optics medians to the registry when due."""
    if _cloud is None:
        return
    try:
        from src import self_characterization
        report = self_characterization.maybe_report()
        if report:
            _cloud.submit_characterization(report)
    except Exception as exc:
        logger.debug("Characterization report skipped: %s", exc)


def _run_survey_only(fits_path: str, cfg: dict) -> None:
    """Contributor path: no telescope, no plan, no target — just harvest
    every star in the frame and ship the source list to the cloud."""
    result = _run_survey_pipeline(fits_path, cfg)
    if not result:
        logger.info("Survey pipeline produced no result for %s",
                    os.path.basename(fits_path))
        return
    sources = result.pop("survey_sources", [])
    logger.info("Survey-only frame: %d sources from %s",
                len(sources), result.get("fits_file"))
    if _cloud is not None and sources:
        from src.calibration_identity import (response_descriptor, response_family,
                                              response_fingerprint)
        result["response_fingerprint"] = response_fingerprint(
            cfg, str(getattr(_cloud, "_node_id", "") or ""))
        result["response_family"] = response_family(cfg)
        result["response_descriptor"] = response_descriptor(
            cfg, str(getattr(_cloud, "_node_id", "") or ""))
        result["gain"] = (cfg.get("photometry") or {}).get("gain")
        result["binning"] = (cfg.get("camera") or {}).get("binning", 1)
        result["_raw_fits_path"] = str(fits_path)
        _cloud.submit_survey(result, sources)
    _maybe_report_characterization()


def _run_photometry_bg(fits_path: str) -> None:
    """Run the photometry pipeline in a background thread and store the result."""
    with _state_lock:
        _state["photometry"]["running"] = True
    try:
        cfg = _load_config()
        if (cfg.get("photometry", {}).get("survey_enabled", False)
                and not _frame_has_target(fits_path, cfg)):
            _run_survey_only(fits_path, cfg)
            return
        result = _run_photometry(fits_path, cfg)
        # Survey sources ride the result out of the pipeline but travel to the
        # cloud on their own endpoint — pop them before the result is stored in
        # dashboard state or the measurement payload (up to ~800 entries).
        survey_sources = result.pop("survey_sources", []) if result else []
        with _state_lock:
            _state["photometry"]["last_result"] = result
            if result:
                hist = _state["photometry"]["history"]
                hist.append({
                    "target_name":  result["target_name"],
                    "bjd":          result["bjd"],
                    "magnitude":    result["magnitude"],
                    "uncertainty":  result["uncertainty"],
                    "quality_flag": result["quality_flag"],
                    "sky_mag":      result.get("sky_mag"),
                })
                if len(hist) > 20:
                    del hist[:-20]
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
                from src.calibration_identity import response_fingerprint
                result["response_fingerprint"] = response_fingerprint(
                    cfg, str(getattr(_cloud, "_node_id", "") or ""))
                _cloud.submit_measurement(
                    result, conditions=_cloud_conditions(), fits_path=fits_path)
                if survey_sources:
                    from src.calibration_identity import (response_descriptor, response_family,
                                                          response_fingerprint)
                    _cloud.submit_survey(
                        {
                            "bjd":        result.get("bjd"),
                            "filter":     result.get("filter"),
                            "zero_point": result.get("zero_point"),
                            "zp_scatter": result.get("zp_scatter"),
                            "fwhm":       result.get("fwhm"),
                            "airmass":    result.get("airmass"),
                            "fits_file":  result.get("fits_file"),
                            "date_obs":   result.get("date_obs"),
                            "item_id":    result.get("item_id"),
                            "bundle_id":  result.get("bundle_id"),
                            "processing_mode": ("event_tile" if result.get("item_id", "").startswith("task_")
                                                else "survey"),
                            "_raw_fits_path": str(fits_path),
                            "response_fingerprint": response_fingerprint(
                                cfg, str(getattr(_cloud, "_node_id", "") or "")),
                            "response_family": response_family(cfg),
                            "response_descriptor": response_descriptor(
                                cfg, str(getattr(_cloud, "_node_id", "") or "")),
                            "gain": (cfg.get("photometry") or {}).get("gain"),
                            "binning": (cfg.get("camera") or {}).get("binning", 1),
                        },
                        survey_sources,
                    )
                _maybe_report_characterization()
        else:
            logger.warning("Photometry pipeline returned no result for %s",
                           os.path.basename(fits_path))
            _telemetry.event("photometry_failed", severity="warning",
                             detail={"file": os.path.basename(fits_path),
                                     "reason": "pipeline returned no result"})
    except Exception as exc:
        logger.error("Photometry pipeline crashed: %s", exc)
        _telemetry.event("photometry_failed", severity="error",
                         detail={"file": os.path.basename(fits_path),
                                 "reason": str(exc)[:300]})
    finally:
        with _state_lock:
            _state["photometry"]["running"] = False


# ── Cloud communicator ─────────────────────────────────────────────────────────

_cloud: Optional[CloudCommunicator] = None
_commissioning: Optional[CommissioningManager] = None
_interrupt_queue: queue.Queue = queue.Queue()


def _cloud_conditions() -> dict:
    """Local conditions snapshot included with cloud heartbeats.

    Surfaced to members via the cloud API so the mobile app can show live
    node status without opening the Node Agent dashboard.
    """
    out: dict = {}
    if _safety_mgr is not None:
        try:
            s = _safety_mgr.status()
            out["safe"] = s.get("safe")
            out["reason"] = s.get("reason", "")
            out["sun_elevation"] = s.get("sun_elevation")
            out["dawn_threshold"] = s.get("dawn_threshold")
            out["heartbeat_ok"] = s.get("heartbeat_ok")
        except Exception:
            pass
    with _sched_lock:
        out["schedule_running"] = _sched_state["running"]
        out["schedule_target"] = _sched_state.get("current_target", "")
        out["schedule_phase"] = _sched_state.get("current_phase", "")
        out["schedule_frame"] = _sched_state.get("current_frame", 0)
        out["schedule_frames"] = _sched_state.get("total_frames", 0)
        out["schedule_completed"] = _sched_state.get("completed", 0)
        out["schedule_total"] = _sched_state.get("total", 0)
        out["schedule_error"] = _sched_state.get("error")
    with _state_lock:
        out["photometry_enabled"] = _state["photometry"]["enabled"]
        out["telescope_connected"] = _state["telescope"].get("connected", False)
        out["camera_connected"] = _state["camera"].get("connected", False)
    try:
        cfg = _load_config()
        out["auto_run_plans"] = bool(cfg.get("cloud", {}).get("auto_run_plans", False))
    except Exception:
        out["auto_run_plans"] = False
    if _cloud is not None:
        try:
            out["cloud_registered"] = bool(_cloud.status.get("registered"))
            out["last_plan_id"] = _cloud.status.get("last_plan_id")
            out["plan_items"] = _cloud.status.get("plan_items", 0)
            out["last_heartbeat_ok"] = _cloud.status.get("last_heartbeat_ok")
        except Exception:
            pass
    try:
        # Structured evidence: recent warning+ events, counters, disk space —
        # lands in the cloud's nodes.last_conditions so a remote operator can
        # see *why* a night failed without touching the member's machine.
        out["events"] = _telemetry.heartbeat_summary()
    except Exception:
        pass
    try:
        if _commissioning is not None:
            out["commissioning"] = _commissioning.status()
    except Exception:
        pass
    try:
        # Read-only status for the app -- there's deliberately no member
        # control over this (see _on_cloud_location): it's automatic.
        with _scan_lock:
            out["horizon_scan"] = {
                "running": _scan_state["running"],
                "have_result": _scan_state["result"] is not None,
                "error": _scan_state["error"],
            }
    except Exception:
        pass
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


def _cloud_state() -> dict:
    """Second-scale live phase for the live fleet map + fast heartbeat.

    Maps the scheduler's internal phase to the cloud's live-state vocabulary and
    reports darkness/sky so the cloud knows, in seconds, which nodes are dark
    and observable (the pool reflow/reflex draw from). Read-only snapshot — never
    touches scheduler or safety state.
    """
    _PHASE_MAP = {
        "slewing": "slewing", "exposing": "exposing",
        "stacking": "stacking", "waiting": "idle",
        "done": "idle", "cancelled": "idle", "": "idle",
    }
    out: dict = {"phase": "idle"}
    is_dark = False
    if _safety_mgr is not None:
        try:
            s = _safety_mgr.status()
            sun = s.get("sun_elevation")
            thr = s.get("dawn_threshold")
            if isinstance(sun, (int, float)) and isinstance(thr, (int, float)):
                is_dark = sun < thr
            out["sky_clear"] = 1.0 if s.get("safe") else 0.0
            reason = str(s.get("reason", ""))
            if s.get("safe") is False and reason.startswith("cloud connection lost"):
                # This is a lost connection to our cloud *backend* server, not an
                # actual sky-weather signal — there's no real cloud sensor here.
                # "clouded" is reserved for a genuine weather-cloud event, so
                # report this dropout as "parked" instead of mislabeling it.
                out["phase"] = "parked"
        except Exception:
            pass
    out["is_dark"] = is_dark
    with _sched_lock:
        running = _sched_state.get("running")
        phase = _sched_state.get("current_phase", "")
        if running:
            out["phase"] = _PHASE_MAP.get(phase, "idle")
            out["target_name"] = _sched_state.get("current_target", "")
            idx = _sched_state.get("current_idx")
            if isinstance(idx, int) and idx >= 0:
                out["plan_item_idx"] = idx
    if out["phase"] == "idle" and not is_dark:
        out["phase"] = "daylight"
    if _work_starved.is_set() and is_dark:
        # Cloud reflow's detect_starved requires this explicit flag — an idle
        # phase alone races between plan items and means nothing.
        out["detail"] = {"work_starved": True}
    return out


_cloud_disconnect_since: Optional[float] = None
_cloud_disconnect_parked: bool = False
_offline_autonomy_was_active: bool = False
_autonomy_clock_reference: Optional[tuple[float, float]] = None


def _cloud_disconnect_monitor_loop() -> None:
    """Park the telescope when cloud heartbeats fail beyond the configured timeout."""
    global _cloud_disconnect_since, _cloud_disconnect_parked
    while True:
        time.sleep(30)
        try:
            _cloud_disconnect_tick()
        except Exception as exc:
            # This watchdog must survive anything (a corrupt config used to
            # kill it permanently and silently).
            logger.error("Cloud disconnect monitor tick failed: %s", exc)


def _offline_autonomy_resume_loop() -> None:
    """Resume a durable signed plan after a process/host restart and outage."""
    last_started = ""
    while True:
        time.sleep(15)
        if _cloud is None or _cloud.status.get("last_heartbeat_ok") is not False:
            continue
        bundle = _cloud.active_autonomy_bundle()
        if not bundle or bundle.get("bundle_id") == last_started:
            continue
        with _sched_lock:
            if _sched_state.get("running"):
                continue
        items = _cloud.autonomy_remaining_items()
        if not items:
            continue
        for item in items:
            item["bundle_id"] = bundle["bundle_id"]
        last_started = bundle["bundle_id"]
        logger.warning("Resuming %d signed plan items while cloud is unavailable", len(items))
        _on_cloud_plan(items)


def _cloud_disconnect_tick() -> None:
    """One evaluation of the cloud-disconnect park policy (called every 30 s)."""
    global _cloud_disconnect_since, _cloud_disconnect_parked
    global _offline_autonomy_was_active, _autonomy_clock_reference
    cfg = _load_config().get("cloud", {})
    timeout = float(cfg.get("disconnect_park_timeout", 1800))
    if timeout <= 0 or _cloud is None or not _cloud.status.get("registered"):
        _cloud_disconnect_since = None
        _cloud_disconnect_parked = False
        return

    ok = _cloud.status.get("last_heartbeat_ok")
    if ok is True:
        if _cloud_disconnect_since is not None:
            logger.info("Cloud heartbeat restored — disconnect timer cleared")
        _cloud_disconnect_since = None
        _cloud_disconnect_parked = False
        _offline_autonomy_was_active = False
        _autonomy_clock_reference = (time.time(), time.monotonic())
        return

    if ok is not False:
        return

    # A verified, unexpired plan plus a recently qualified clock authorizes
    # bounded continuation.  It never relaxes any local safety gate.
    wall, mono = time.time(), time.monotonic()
    if _autonomy_clock_reference is not None:
        if abs((wall - _autonomy_clock_reference[0])
               - (mono - _autonomy_clock_reference[1])) > 5.0:
            _cloud.invalidate_clock_trust("wall/monotonic discontinuity while offline")
    _autonomy_clock_reference = (wall, mono)
    active_bundle = _cloud.active_autonomy_bundle()
    disk_free = _telemetry.disk_free_gb(".")
    outbox_full = bool(getattr(_cloud, "outbox_at_capacity", lambda: False)())
    if (active_bundle is not None and not outbox_full
            and (disk_free is None or disk_free >= 1.0)):
        if _cloud_disconnect_since is not None:
            logger.info("Cloud offline — continuing verified autonomy bundle")
        _cloud_disconnect_since = None
        _cloud_disconnect_parked = False
        _offline_autonomy_was_active = True
        return

    if _offline_autonomy_was_active:
        # Expiry, a clock-trust loss, or exhausted storage ends autonomy now;
        # the ordinary 30-minute grace is only for nodes that never had a bundle.
        _offline_autonomy_was_active = False
        _cloud_disconnect_since = time.monotonic() - timeout

    now = time.monotonic()
    if _cloud_disconnect_since is None:
        _cloud_disconnect_since = now
        logger.warning(
            "Cloud heartbeat failing — will park after %ds without contact",
            int(timeout),
        )
        return

    elapsed = now - _cloud_disconnect_since
    if elapsed < timeout or _cloud_disconnect_parked:
        return

    _cloud_disconnect_parked = True
    reason = f"cloud connection lost >{int(timeout)}s"
    logger.critical("Cloud disconnect timeout — emergency park (%s)", reason)
    _telemetry.event("cloud_disconnect_park", severity="critical",
                     detail={"timeout_s": int(timeout)})
    if _safety_mgr is not None:
        _safety_mgr.emergency_park(reason)
    elif _tel is not None:
        _on_safety_unsafe()

        def _park() -> None:
            try:
                with _device_lock:
                    _tel.park()
                logger.info("Park complete after cloud disconnect")
            except Exception as exc:
                logger.error("Park after cloud disconnect failed: %s", exc)

        threading.Thread(target=_park, daemon=True, name="cloud-disco-park").start()


def _on_cloud_plan(items: list, contingencies: Optional[dict] = None) -> None:
    """A new observation plan arrived from the cloud.  Validate it with the
    same gate as /api/schedule/run; execute it only when auto_run_plans is on
    and no schedule is already running.  The plan's contingency alternates are
    stored (validated) for local gap/failure/end-of-plan fill."""
    valid, err = _validate_schedule_items(items)
    if err is not None:
        logger.warning("Cloud plan rejected by validator: %s", err)
        _telemetry.event("plan_rejected", severity="error",
                         detail={"reason": err, "items": len(items)})
        return
    _store_alternates(contingencies or {})
    _work_starved.clear()
    _telemetry.event("plan_received", severity="info",
                     detail={"items": len(valid)})
    cfg = _load_config()
    if not cfg.get("cloud", {}).get("auto_run_plans", True):
        logger.warning("Cloud plan received (%d items) — auto_run_plans is off, "
                        "node will NOT observe until this is enabled or the "
                        "plan is started manually from the dashboard", len(valid))
        if _cloud is not None:
            _cloud.status["plan_pending_review"] = True
        _telemetry.event("plan_deferred_auto_run_off", severity="warning",
                         detail={"items": len(valid)})
        return
    if _cloud is not None:
        _cloud.status["plan_pending_review"] = False
    with _sched_lock:
        if _sched_state["running"]:
            # A newer cloud plan supersedes an older one that hasn't actually
            # started observing yet (still waiting for darkness or a start
            # time).  An actively exposing schedule is never pre-empted here.
            if (_sched_state.get("source") == "cloud"
                    and _sched_state.get("current_phase")
                    in ("starting", "waiting_for_dark", "waiting")):
                logger.info("New cloud plan supersedes the waiting one — replacing")
                _sched_state["cancelled"] = True
            else:
                logger.info("Cloud plan received but a schedule is already "
                            "running — not starting it")
                return
    # If we superseded, give the old runner a moment to unwind.
    for _ in range(100):
        with _sched_lock:
            if not _sched_state["running"]:
                break
        time.sleep(0.1)
    with _sched_lock:
        if _sched_state["running"]:
            logger.warning("Previous schedule did not stop — new plan not started")
            return
    threading.Thread(
        target=_run_schedule_bg, args=(valid,),
        kwargs={"source": "cloud", "wait_for_dark": True},
        daemon=True, name="sched-runner-cloud",
    ).start()
    logger.info("Cloud plan started: %d observations", len(valid))


def _choose_interrupt_exposure(mag: Optional[float]) -> tuple:
    """Pick (expDur, expCount) for an interrupt target sized to its magnitude."""
    max_exp = 30.0
    if mag is None:
        mag = 13.0
    if mag < 9.0:
        dur, total_min = 5.0, 5.0
    elif mag < 11.0:
        dur, total_min = 10.0, 8.0
    elif mag < 13.0:
        dur, total_min = 15.0, 12.0
    else:
        dur, total_min = 30.0, 20.0
    dur = min(dur, max_exp)
    count = max(5, int(round(total_min * 60.0 / dur)))
    return dur, count


def _on_cloud_interrupt(item: dict) -> None:
    """Handle a cloud interrupt: build a schedule item and queue it."""
    is_event_task = item.get("task_type") == "event_tile"
    name = item.get("target") if is_event_task else item.get("name", "Unknown")
    ra   = item.get("ra")   # decimal hours (cloud sends this alongside ra_deg)
    dec  = item.get("dec")  # degrees
    mag  = item.get("mag")
    time_critical = bool(item.get("time_critical", True))

    if ra is None or dec is None:
        logger.warning("Interrupt %s missing ra/dec — skipping", name)
        return

    if is_event_task:
        exp_dur = float(item.get("expDur") or 30)
        exp_count = int(item.get("expCount") or 10)
    else:
        exp_dur, exp_count = _choose_interrupt_exposure(mag)
    sched_item = {
        "target":   name,
        "ra":       float(ra),
        "dec":      float(dec),
        "expDur":   exp_dur,
        "expCount": exp_count,
        "binning":  1,
        "notes":    f"interrupt: {item.get('reason', '')}",
        "item_id": item.get("item_id", ""),
        "task_id": item.get("task_id", ""),
        "starts_at_utc": item.get("starts_at_utc", ""),
        "latest_start_utc": item.get("latest_start_utc", ""),
        "task_type": item.get("task_type", "science"),
        "campaign_id": item.get("campaign_id", ""),
        "priority": item.get("priority", 0),
        "cancellation_generation": item.get("cancellation_generation", 0),
        "filter": item.get("filter", ""),
    }

    # Interrupts drive the mount exactly like plan items — hold them to the
    # same coordinate/exposure bounds instead of trusting the payload.
    valid, err = _validate_schedule_items([sched_item])
    if err is not None:
        logger.warning("Interrupt %s rejected by validator: %s", name, err)
        _telemetry.event("interrupt_rejected", severity="warning",
                         target=name, detail={"reason": err})
        return
    sched_item = valid[0]

    logger.warning("Interrupt queued: %s (%.1fs × %d, time_critical=%s)",
                   name, exp_dur, exp_count, time_critical)
    _work_starved.clear()   # new work arrived — no longer starved
    _interrupt_queue.put_nowait(sched_item)

    if time_critical:
        with _sched_lock:
            if _sched_state["running"]:
                if is_event_task and _sched_state.get("current_phase") == "exposing":
                    logger.info("Event tile queued until active exposure completes")
                    _sched_state["cancel_after_frame"] = True
                else:
                    logger.warning("Pre-empting running schedule for %s", name)
                    _sched_state["cancelled"] = True


def _on_cloud_task_cancel(task_id: str) -> None:
    """Stop a superseded event task between frames, never mid-exposure."""
    with _sched_lock:
        if str(_sched_state.get("current_task_id") or "") != str(task_id):
            return
        if _sched_state.get("current_phase") == "exposing":
            _sched_state["cancel_after_frame"] = True
        else:
            _sched_state["cancelled"] = True


def _interrupt_dispatcher_loop() -> None:
    """Daemon thread: wait for queued interrupt observations and run them."""
    while True:
        item = _interrupt_queue.get()          # block until one arrives
        items = [item]
        while not _interrupt_queue.empty():    # drain any that piled up
            try:
                items.append(_interrupt_queue.get_nowait())
            except queue.Empty:
                break

        if _cloud is not None:
            items = [it for it in items
                     if not (it.get("task_id") and _cloud.task_cancelled(it["task_id"]))]
        if not items:
            continue

        # Wait for any running schedule to finish before starting ours.
        while True:
            with _sched_lock:
                if not _sched_state["running"]:
                    break
            time.sleep(1)

        logger.info("Interrupt dispatcher: running %d interrupt observation(s)", len(items))
        _run_schedule_bg(items, source="interrupt", wait_for_dark=True)


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
    # Dawn parks are routine; anything else is an emergency worth flagging.
    routine = str(reason).startswith("dawn")
    _telemetry.event("emergency_park", severity="info" if routine else "critical",
                     detail={"reason": reason})


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
        if _sched_cancelled():
            return False
        try:
            if _tel and _tel.is_slewing():
                break
        except Exception:
            pass
        time.sleep(0.25)
    # Now wait for it to finish. A mount stuck reporting Slewing=True must
    # not make the schedule unabortable: without the cancel check, an abort
    # request sat ignored for the full timeout while the thread spun here.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _sched_cancelled():
            logger.info("Slew wait abandoned: schedule cancelled")
            return False
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


def _on_cloud_location(lat: float, lon: float) -> None:
    """Cloud told us the node's current effective observer location (a
    portable node's active session site, or its fixed coordinates). The
    node agent has no other way to learn this -- it only knows whatever
    static lat/lon is in its own config.yaml otherwise, which is exactly
    what left a just-moved portable node's own safety/dawn calculations
    silently wrong until a manual fix.

    If the location actually changed: update local config + the live
    safety manager to match, and kick off a horizon re-scan in the
    background. Deliberately no manual control here -- a portable node's
    obstructions are different at every site, so this should just happen
    rather than requiring the member to notice and trigger it themselves.
    """
    try:
        cfg = _load_config()
        if "safety" not in cfg or cfg["safety"] is None:
            cfg["safety"] = {}
        observer = cfg["safety"].get("observer") or {}
        old_lat, old_lon = observer.get("latitude"), observer.get("longitude")
        moved = (
            old_lat is None or old_lon is None
            or abs(float(old_lat) - lat) > 0.01
            or abs(float(old_lon) - lon) > 0.01
        )
        cfg["safety"]["observer"] = {**observer, "latitude": lat, "longitude": lon}
        with open("config.yaml", "w") as fh:
            yaml.dump(cfg, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
        if _safety_mgr is not None:
            _safety_mgr.set_observer(lat, lon)
    except Exception as exc:
        logger.warning("Could not update local observer location: %s", exc)
        return

    if not moved:
        return
    logger.info(
        "Observer location changed to (%.4f, %.4f) — starting horizon scan", lat, lon)
    with _scan_lock:
        if _scan_state["running"]:
            logger.info("Horizon scan already running — skipping")
            return
    threading.Thread(
        target=_auto_horizon_scan, daemon=True, name="auto-horizon-scan"
    ).start()


def _on_cloud_dry_run(enabled: bool) -> None:
    """Cloud told us this node's admin dry-run testing mode changed (see
    cloud/registry.py::dry_run_active). While enabled the safety watchdog
    ignores actual sun position, so a full night run — real slews, real
    exposures — can be exercised in daylight. Applied only to the live
    manager; not persisted to config.yaml since it's a bounded, cloud-owned
    testing window rather than a standing local setting."""
    if _safety_mgr is not None:
        _safety_mgr.set_dry_run(enabled)


# ── Tonight's intent ───────────────────────────────────────────────────────────
#
# The cloud decides each night whether this telescope observes: the member
# accepted, declined, said nothing (and the recommendation runs), stood the node
# down, or the weather closed it. See cloud/nightly.py. The node holds the last
# answer so it keeps behaving sensibly when the cloud is unreachable.

_tonight_lock = threading.Lock()
_tonight: dict = {}


def _on_cloud_tonight(intent: dict) -> None:
    """Tonight's intent changed. Act on it immediately.

    Called only on a change, so cancelling here does not fight the scheduler on
    every poll. A member standing their telescope down is an instruction: the
    running schedule is cancelled and the mount parked, rather than allowed to
    finish the current target first.
    """
    with _tonight_lock:
        _tonight.clear()
        _tonight.update(intent or {})

    if intent.get("observing"):
        logger.info("Tonight: observing (%s)", intent.get("status", ""))
        return

    reason = intent.get("reason") or intent.get("status") or "no reason given"
    logger.warning("Tonight: not observing — %s", reason)

    with _sched_lock:
        was_running = _sched_state["running"]
        if was_running:
            _sched_state["cancelled"] = True

    _telemetry.event("tonight_stand_down",
                     severity="info",
                     detail={"status": intent.get("status", ""),
                             "reason": str(reason)[:200],
                             "schedule_was_running": was_running})

    if was_running:
        # Stop the exposure in progress rather than waiting for it to finish;
        # a stand-down usually means something is wrong with the telescope.
        try:
            if _cam is not None:
                _cam.abort_exposure()
        except Exception as exc:
            logger.debug("Abort on stand-down failed: %s", exc)
        try:
            if _tel is not None:
                with _device_lock:
                    _tel.park()
                logger.info("Parked on stand-down")
        except Exception as exc:
            logger.debug("Park on stand-down failed: %s", exc)


def _tonight_intent() -> dict:
    with _tonight_lock:
        return dict(_tonight)


def _tonight_allows_observing() -> bool:
    """Whether tonight's intent permits observing right now.

    Defaults to True before the cloud has answered: a node that has never heard
    otherwise behaves as it always has, and the SafetyManager still decides
    whether it is actually safe to open.
    """
    intent = _tonight_intent()
    if not intent:
        return True
    return bool(intent.get("observing"))


#: Object types worth looking at, in the order we would rather image them.
#: These are pyongc's full type names, as they appear in _dso_catalog -- the
#: abbreviations you might expect ("PN", "GCl") do not occur.
#:
#: Deliberately not ordered by brightness: someone who asked for an imaging
#: programme wants something that reads as a picture. A nebula or a globular
#: does that; the 9,793 mostly-anonymous galaxies in the catalogue do not,
#: which is why Galaxy sits near the end rather than dominating by count.
_IMAGING_TYPES = (
    "Emission Nebula",
    "Reflection Nebula",
    "Planetary Nebula",
    "Star cluster + Nebula",
    "Supernova remnant",
    "HII Ionized region",
    "Nebula",
    "Globular Cluster",
    "Open Cluster",
    "Galaxy",
    "Galaxy Pair",
    "Galaxy Triplet",
    "Group of galaxies",
)

#: Objects with a Messier number first, then NGC. A member recognises M51.
def _imaging_rank(obj: dict) -> tuple:
    obj_id = str(obj.get("id") or "")
    named = 0 if obj_id.startswith("M") and obj_id[1:].isdigit() else 1
    try:
        type_rank = _IMAGING_TYPES.index(str(obj.get("type") or ""))
    except ValueError:
        type_rank = len(_IMAGING_TYPES)
    return (named, type_rank, obj_id)


def _pick_imaging_target() -> Optional[dict]:
    """A catalogue object worth imaging that is safely reachable right now.

    Reuses _slew_rejection so the imaging half of the night obeys exactly the
    same horizon mask and safety state as the research half -- an unattended
    slew must not be able to reach somewhere a scheduled one could not.
    """
    candidates = [o for o in _dso_catalog
                  if str(o.get("type") or "") in _IMAGING_TYPES]
    for obj in sorted(candidates, key=_imaging_rank):
        try:
            if _slew_rejection(float(obj["ra"]), float(obj["dec"])) is None:
                return obj
        except (TypeError, ValueError, KeyError):
            continue
    return None


def _run_imaging_block(target: Optional[dict] = None) -> None:
    """Point at something and build a stacked image until the night ends.

    Runs when a bounded research block finishes and the member asked for
    imaging afterwards. Nobody is awake for this, so it is deliberately
    unambitious: pick one target, centre it, stack, and stop the moment safety
    says so. It never re-slews looking for something better -- a mount hunting
    around an empty sky at 3am is worse than a shorter stack.
    """
    if target is None:
        target = _pick_imaging_target()
    if target is None:
        logger.info("Imaging: nothing reachable right now — skipping")
        _telemetry.event("imaging_no_target", severity="info", detail={})
        return

    name = str(target.get("id") or "?")
    ra, dec = float(target["ra"]), float(target["dec"])

    reason = _slew_rejection(ra, dec)
    if reason:
        logger.info("Imaging: %s not reachable (%s)", name, reason)
        return

    logger.info("Imaging: starting on %s", name)
    _telemetry.event("imaging_started", severity="info",
                     detail={"target": name, "ra": ra, "dec": dec})
    with _imaging_lock:
        _imaging_state.update({"running": True, "target": name,
                               "started_at": time.time(), "error": None})
    cfg = _load_config()
    centering = cfg.get("centering", {}) or {}
    stacking = cfg.get("stacking", {}) or {}

    def _num(source, key, default, cast):
        try:
            return cast(source.get(key, default))
        except (TypeError, ValueError):
            return cast(default)

    # The slew is fatal and the centring is not, so they cannot share a
    # handler: folded together, a mount that failed to move would fall through
    # to "stacking anyway" and spend the night imaging whatever it happened to
    # be pointing at, reporting success.
    try:
        with _device_lock:
            # slew_to_coordinates takes RA in HOURS, which is how the catalogue
            # stores it. _run_centering_bg takes DEGREES. Getting that wrong
            # points the telescope fifteen times too far round the sky.
            _tel.slew_to_coordinates(ra, dec)
    except Exception as exc:
        logger.warning("Imaging: could not slew to %s: %s", name, exc)
        with _imaging_lock:
            _imaging_state.update({"running": False, "error": str(exc)[:300]})
        _telemetry.event("imaging_failed", severity="warning",
                         detail={"target": name, "error": str(exc)[:200]})
        return

    try:
        _run_centering_bg(
            ra * 15.0, dec,
            _num(centering, "exposure_s", 3.0, float),
            _num(centering, "tolerance_arcmin", 3.0, float),
            _num(centering, "max_iterations", 4, int),
            _num(centering, "settle_s", 2.0, float),
        )
    except Exception as exc:
        # Centring is an improvement, not a requirement: an uncentred frame is
        # still a frame, and giving up here would waste the rest of the night.
        logger.info("Imaging: centring on %s failed (%s) — stacking anyway",
                    name, exc)

    try:
        exposure_s = _num(stacking, "exposure_s", 10.0, float)
        # Enough frames to run until dawn; the loop below is what actually
        # ends the block, on safety or a stand-down.
        n_frames = max(1, int((10 * 3600) / max(1.0, exposure_s)))
        threading.Thread(
            target=_run_stacking_bg,
            args=(n_frames, exposure_s,
                  max(1, _num(stacking, "preview_every", 1, int))),
            daemon=True, name="imaging-stack",
        ).start()
    except Exception as exc:
        logger.warning("Imaging: could not start on %s: %s", name, exc)
        with _imaging_lock:
            _imaging_state.update({"running": False, "error": str(exc)[:300]})
        _telemetry.event("imaging_failed", severity="warning",
                         detail={"target": name, "error": str(exc)[:200]})
        return

    # Hold until safety, a stand-down, or dawn ends it. Checked on the same
    # cadence the scheduler uses so a stand-down stops imaging as fast as it
    # stops a research run.
    while not _sched_cancelled() and _tonight_allows_observing():
        if _safety_mgr is not None and not _safety_mgr.is_safe():
            logger.info("Imaging: stopping — %s",
                        _safety_mgr.status().get("reason") or "unsafe")
            break
        time.sleep(5)

    with _imaging_lock:
        _imaging_state["running"] = False
    logger.info("Imaging: finished on %s", name)
    _telemetry.event("imaging_finished", severity="info", detail={"target": name})


_imaging_lock = threading.Lock()
_imaging_state: dict = {"running": False, "target": "", "started_at": None,
                        "error": None}


def imaging_status() -> dict:
    with _imaging_lock:
        return dict(_imaging_state)


def _research_window_expired() -> bool:
    """True once tonight's research block has run its allotted hours.

    Only meaningful when the member asked for a bounded research block with
    imaging afterwards. An unbounded night never expires.
    """
    intent = _tonight_intent()
    proposal = intent.get("proposal") or {}
    if not proposal.get("imaging_after"):
        return False
    try:
        hours = float(proposal.get("research_hours") or 0.0)
    except (TypeError, ValueError):
        return False
    if hours <= 0:
        return False

    with _sched_lock:
        started = _sched_state.get("started_at")
    if not started:
        return False
    return (time.time() - float(started)) >= hours * 3600.0


def _auto_horizon_scan() -> None:
    """Default-parameter horizon scan, applied automatically on completion.

    Unlike the manual API (POST /api/safety/horizon-scan), this writes the
    result straight into config.yaml's horizon_mask -- there's no UI for a
    member to review/apply it, by design (see _on_cloud_location).
    """
    if _tel is None or _cam is None:
        logger.info("Skipping auto horizon scan: telescope/camera not connected")
        return
    _run_horizon_scan(
        floor_alt=25.0, start_alt=60.0, step_deg=5.0,
        exposure_s=5.0, star_threshold=5, settle_s=2.0,
    )
    with _scan_lock:
        result = _scan_state.get("result")
        error = _scan_state.get("error")
    if error or not result:
        logger.warning("Auto horizon scan produced no usable mask: %s", error)
        return
    try:
        cfg = _load_config()
        if "safety" not in cfg or cfg["safety"] is None:
            cfg["safety"] = {}
        cfg["safety"]["horizon_mask"] = result
        with open("config.yaml", "w") as fh:
            yaml.dump(cfg, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
        if _safety_mgr is not None:
            _safety_mgr.set_horizon_mask(result)
        logger.info("Auto horizon scan applied new mask: %s", result)
    except Exception as exc:
        logger.warning("Could not persist auto horizon scan result: %s", exc)


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
                        connected=True, error=None, ra=ra, dec=dec,
                        slewing=slewing, parked=parked, tracking=tracking,
                        arm_state=arm_state,
                    )
                if _tel_connected_prev is False:
                    logger.info("Telescope connection restored")
                _tel_connected_prev = True
            except Exception as exc:
                with _state_lock:
                    _tel_host = _state["server"]["address"] if _state["server"] else "?"
                    _tel_port = _state["server"]["port"] if _state["server"] else 0
                    _state["telescope"]["connected"] = False
                    _state["telescope"]["error"] = _friendly_conn_error(
                        "telescope", _tel_host, _tel_port, exc)
                if _tel_connected_prev is True:
                    logger.warning("Telescope connection lost: %s", exc)
                _tel_connected_prev = False

        if cam_enabled and _cam is not None:
            try:
                state     = _cam.camera_state()
                img_ready = _cam.image_ready()
                with _state_lock:
                    _state["camera"].update(
                        connected=True, error=None, state=state,
                        state_name=_CAMERA_STATES.get(state, "Unknown"),
                        image_ready=img_ready,
                    )
                if _cam_connected_prev is False:
                    logger.info("Camera connection restored")
                _cam_connected_prev = True
            except Exception as exc:
                with _state_lock:
                    _cam_host = _state["server"]["address"] if _state["server"] else "?"
                    _cam_port = _state["server"]["port"] if _state["server"] else 0
                    _state["camera"]["connected"] = False
                    _state["camera"]["error"] = _friendly_conn_error(
                        "camera", _cam_host, _cam_port, exc)
                if _cam_connected_prev is True:
                    logger.warning("Camera connection lost: %s", exc)
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
                    _state["focuser"].update(connected=True, error=None, position=pos, moving=moving)
            except Exception as exc:
                with _state_lock:
                    _foc_host = _state["server"]["address"] if _state["server"] else "?"
                    _foc_port = _state["server"]["port"] if _state["server"] else 0
                    _state["focuser"]["connected"] = False
                    _state["focuser"]["error"] = _friendly_conn_error(
                        "focuser", _foc_host, _foc_port, exc)

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


_poller_thread: Optional[threading.Thread] = None
_poller_thread_lock = threading.Lock()


def _start_poller() -> None:
    """Start the device poll loop, ensuring only one instance ever runs.

    A quick disconnect→reconnect used to clear _poller_stop before the old
    poller thread observed it, leaving two loops polling the devices forever.
    """
    global _poller_thread
    with _poller_thread_lock:
        old = _poller_thread
        if old is not None and old.is_alive():
            _poller_stop.set()
            # A single hung device call can take up to its 10 s HTTP timeout;
            # wait it out rather than risk a second concurrent poller.
            old.join(timeout=15)
            if old.is_alive():
                # Leave _poller_stop set so the stuck thread exits when it can;
                # the next connect attempt will start a fresh poller.
                logger.warning("Previous poller thread did not exit — not starting another")
                return
        _poller_stop.clear()
        _poller_thread = threading.Thread(
            target=_poll_loop, daemon=True, name="alpaca-poller")
        _poller_thread.start()


# ── Config helper ──────────────────────────────────────────────────────────────

# Last successfully parsed config. A half-written or hand-mangled config.yaml
# must degrade to the previous good config, not crash whichever background
# loop happened to call _load_config() next (the cloud-disconnect watchdog
# reads it every 30 s and used to die permanently on a parse error).
_last_good_config: dict = {}
_config_parse_error_reported = False


def _load_config() -> dict:
    global _last_good_config, _config_parse_error_reported
    try:
        with open("config.yaml") as fh:
            cfg = yaml.safe_load(fh)
        if cfg is not None and not isinstance(cfg, dict):
            raise ValueError("config.yaml root must be a mapping")
        if _config_parse_error_reported:
            _config_parse_error_reported = False
            _telemetry.event("config_parse_recovered", severity="info")
    except FileNotFoundError:
        cfg = {}
    except Exception as exc:
        if not _config_parse_error_reported:
            _config_parse_error_reported = True
            _telemetry.event(
                "config_parse_failed", severity="error",
                detail={"error": str(exc)[:300],
                        "hint": "config.yaml is corrupt — using last good config"})
        cfg = copy.deepcopy(_last_good_config)
    _last_good_config = copy.deepcopy(cfg or {})
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


# ── Setup page ────────────────────────────────────────────────────────────────
# The apps tell people to "open http://localhost:5173 on the computer running
# the node software" — until now that was a 404, and the pairing token existed
# only in the service's stdout, which nobody running it as a background service
# can see. This page is the one place a human can read the token and find out
# what the node is waiting for.

_SETUP_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Telescope Node — Setup</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; --fg:#1a1a1a; --bg:#fff; --muted:#666;
          --card:#f5f5f7; --border:#ddd; --ok:#1a7f37; --warn:#9a6700; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#e8e8e8; --bg:#161618; --muted:#a0a0a8; --card:#232326;
            --border:#3a3a3e; --ok:#3fb950; --warn:#d29922; }
  }
  * { box-sizing: border-box; }
  body { font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         color: var(--fg); background: var(--bg); margin: 0; padding: 2rem 1rem; }
  main { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
  .sub { color: var(--muted); margin: 0 0 1.5rem; }
  .card { background: var(--card); border: 1px solid var(--border);
          border-radius: 12px; padding: 1.25rem; margin-bottom: 1rem; }
  .token { font: 700 2.5rem/1.1 ui-monospace, SFMono-Regular, Menlo, monospace;
           letter-spacing: .06em; margin: .5rem 0; word-break: break-all; }
  .row { display: flex; justify-content: space-between; gap: 1rem;
         padding: .5rem 0; border-bottom: 1px solid var(--border); }
  .row:last-child { border-bottom: 0; }
  .row .k { color: var(--muted); }
  .ok { color: var(--ok); font-weight: 600; }
  .warn { color: var(--warn); font-weight: 600; }
  ol { padding-left: 1.25rem; } li { margin: .4rem 0; }
  button { font: inherit; padding: .5rem .9rem; border-radius: 8px;
           border: 1px solid var(--border); background: var(--bg);
           color: var(--fg); cursor: pointer; }
</style></head>
<body><main>
  <h1>Telescope node</h1>
  <p class="sub">This computer runs the telescope. Keep it awake and online at night.</p>
  <div id="link"></div>
  <div class="card">
    <div class="row"><span class="k">Telescope</span><span id="scope">…</span></div>
    <div class="row"><span class="k">Camera</span><span id="cam">…</span></div>
    <div class="row"><span class="k">Cloud account</span><span id="cloud">…</span></div>
    <div class="row"><span class="k">Node ID</span><span id="nid">…</span></div>
  </div>
  <p class="sub" id="err"></p>
<script>
async function j(p){ const r = await fetch(p); if(!r.ok) throw new Error(p+" -> "+r.status); return r.json(); }
function esc(s){ return String(s==null?"":s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
async function tick(){
  try {
    const [st, cl] = await Promise.all([j("/api/status"), j("/api/cloud")]);
    const linked = cl.enabled && cl.registered;
    document.getElementById("link").innerHTML = linked
      ? '<div class="card"><strong class="ok">Linked to your account.</strong>'
        + '<p class="sub" style="margin:.5rem 0 0">Nothing else to do here — '
        + 'the network will send this telescope work automatically.</p></div>'
      : '<div class="card"><strong>Pairing code</strong>'
        + '<div class="token">' + esc(cl.pair_token || "unavailable") + '</div>'
        + '<ol><li>Open the desktop app on this computer and sign in.</li>'
        + '<li>Choose <strong>Connect telescope</strong>.</li>'
        + '<li>Enter this pairing code when asked.</li></ol>'
        + '<p class="sub">This page updates by itself once the link succeeds — '
        + 'you can leave it open.</p></div>';
    const scope = st.telescope || {}, cam = st.camera || {};
    document.getElementById("scope").innerHTML = scope.connected
      ? '<span class="ok">connected</span>' : '<span class="warn">not connected</span>';
    document.getElementById("cam").innerHTML = cam.connected
      ? '<span class="ok">connected</span>' : '<span class="warn">not connected</span>';
    document.getElementById("cloud").innerHTML = linked
      ? '<span class="ok">linked</span>' : '<span class="warn">waiting to be linked</span>';
    document.getElementById("nid").textContent = cl.node_id || "—";
    document.getElementById("err").textContent = "";
  } catch (e) { document.getElementById("err").textContent = String(e); }
}
tick(); setInterval(tick, 3000);
</script>
</main></body></html>
"""


@app.route("/")
def setup_page():
    return Response(_SETUP_HTML, mimetype="text/html")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    with _state_lock:
        snapshot = copy.deepcopy(_state)
    if _commissioning is not None:
        snapshot["commissioning"] = _commissioning.status()
    return jsonify(snapshot)


@app.route("/api/commissioning")
def api_commissioning():
    if _commissioning is None:
        return jsonify({"status": "unavailable"}), 503
    return jsonify(_commissioning.status())


@app.route("/api/commissioning/restart", methods=["POST"])
def api_commissioning_restart():
    if _commissioning is None:
        return jsonify({"error": "commissioning unavailable"}), 503
    _commissioning.restart()
    return jsonify({"ok": True, **_commissioning.evaluate()})


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


@app.route("/api/logs/recent")
def api_logs_recent():
    """Recent log lines as JSON, newest last. ?lines=N (default 200, max 300).

    /api/logs is a Server-Sent Events stream that never ends -- right for a
    live-tailing dashboard, useless to anything that wants to read the log and
    move on. A non-streaming caller there just blocks until it times out, which
    is exactly what the MCP diagnose tool was doing.
    """
    try:
        lines = int(request.args.get("lines", 200))
    except (TypeError, ValueError):
        lines = 200
    lines = max(1, min(lines, 300))
    with _subscribers_lock:
        entries = list(_log_history)[-lines:]
    return jsonify({"lines": entries, "count": len(entries)})


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


def _friendly_conn_error(device: str, host: str, port: int, exc: Exception) -> str:
    """Turn a raw ALPACA connection exception into an actionable message for the UI."""
    exc_str = str(exc).lower()
    unreachable = (
        "timed out" in exc_str or "timeout" in exc_str or "refused" in exc_str or
        "no route to host" in exc_str or "network is unreachable" in exc_str or
        "connectionerror" in exc_str or "failed to establish a new connection" in exc_str
    )
    if unreachable:
        return (
            f"Can't reach the {device} at {host}:{port}. It looks like the Seestar is "
            f"offline or its network address changed. Check that it's powered on and "
            f"connected to Wi-Fi, then check your router's device list for its current IP "
            f"(it may differ from {host} if it rejoined the network)."
        )
    return f"Can't reach the {device} at {host}:{port}: {exc}"


@app.route("/api/connect", methods=["POST"])
def api_connect():
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

    body, status = _do_connect(host, port,
                               set_as_default=bool(data.get("set_as_default", False)))
    return jsonify(body), status


def _do_connect(host: str, port: int, set_as_default: bool = False) -> tuple[dict, int]:
    """Connect telescope/camera/focuser/cover at *host:port*.

    Shared by the /api/connect route and the headless NodeSupervisor
    reconnect path, so a service restart at 2 a.m. re-establishes devices
    without anyone opening the browser dashboard.
    """
    global _tel, _cam, _cover, _foc, _manual_disconnect
    _manual_disconnect = False
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
                    enabled=True, connected=True, error=None,
                    device_name=tel_device_name, serial=tel_serial,
                )
            if tel_serial:
                try:
                    from src.config_patch import apply_config_patch
                    obs_patch: dict = {"telescope_serial": tel_serial}
                    if tel_device_name:
                        obs_patch["telescope_name"] = tel_device_name
                    apply_config_patch({"observatory": obs_patch})
                    logger.info("Telescope identified: %s (serial: %s)", tel_device_name, tel_serial)
                except Exception as exc:
                    logger.warning("Could not save telescope serial: %s", exc)
            if _safety_mgr is not None:
                _safety_mgr.attach_telescope(_tel)
        except Exception as exc:
            logger.error("Telescope connect failed: %s", exc)
            friendly = _friendly_conn_error("telescope", host, port, exc)
            errors.append(friendly)
            with _state_lock:
                _state["telescope"]["error"] = friendly
            _tel = None

    if devices.get("camera", {}).get("enabled", False):
        num = devices["camera"].get("device_number", 0)
        try:
            _cam = Camera(host, port, num, api_ver)
            _cam.connect()
            cam_ok = True
            with _state_lock:
                _state["camera"].update(enabled=True, connected=True, error=None)
        except Exception as exc:
            logger.error("Camera connect failed: %s", exc)
            friendly = _friendly_conn_error("camera", host, port, exc)
            errors.append(friendly)
            with _state_lock:
                _state["camera"]["error"] = friendly
            _cam = None

    if devices.get("focuser", {}).get("enabled", False):
        num = devices["focuser"].get("device_number", 0)
        try:
            _foc = Focuser(host, port, num, api_ver)
            _foc.connect()
            with _state_lock:
                _state["focuser"].update(enabled=True, connected=True, error=None,
                                         position=_foc.position())
        except Exception as exc:
            logger.warning("Focuser connect failed (autofocus unavailable): %s", exc)
            with _state_lock:
                _state["focuser"]["error"] = _friendly_conn_error("focuser", host, port, exc)
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
        if _cloud is not None:
            _cloud.request_heartbeat()
        return {"error": "No devices connected — " + "; ".join(errors)}, 502

    with _state_lock:
        _state["connected"] = True
    if _cloud is not None:
        _cloud.request_heartbeat()

    if set_as_default:
        try:
            from src.config_patch import apply_config_patch
            apply_config_patch(
                {"alpaca": {"default_server": {"address": host, "port": port}}})
            logger.info("Default ALPACA server set to %s:%d", host, port)
        except Exception as exc:
            logger.warning("Could not save default server: %s", exc)

    _parts = []
    if tel_ok:
        _parts.append("telescope")
    if cam_ok:
        _parts.append("camera")
    logger.info("Connected to %s:%d — %s", host, port, " + ".join(_parts))
    if errors:
        logger.warning("Connection warnings: %s", "; ".join(errors))
    _start_poller()
    threading.Thread(target=_auto_mount_and_watch, args=(host,), daemon=True,
                     name="smb-automount").start()
    return {"ok": True, "telescope": tel_ok, "camera": cam_ok, "errors": errors}, 200


# ── Supervisor glue ────────────────────────────────────────────────────────────
# True after an explicit user disconnect: the supervisor must not fight the
# member by silently reconnecting hardware they chose to release.
_manual_disconnect = False


def _supervisor_devices_ok() -> bool:
    """Whether the supervisor should consider device connectivity handled."""
    if _manual_disconnect:
        return True  # user chose to disconnect — leave it alone
    return _tel is not None or _cam is not None


def _supervisor_connect(host: str, port: int) -> bool:
    _body, status = _do_connect(host, port)
    return status == 200


def _supervisor_watcher_ok() -> bool:
    if _image_watcher is None:
        return False
    return (bool(getattr(_image_watcher, "_running", False))
            and os.path.isdir(getattr(_image_watcher, "_path", "")))


def _revive_image_watcher() -> bool:
    """Re-mount the Seestar share if possible and restart the image watcher."""
    cfg = _load_config()
    iw_cfg = cfg.get("image_watcher", {}) or {}
    if not iw_cfg.get("enabled", False):
        return True
    with _state_lock:
        srv = _state.get("server") or {}
    host = srv.get("address") or cfg.get("alpaca", {}).get("default_server", {}).get("address")
    if host:
        mount_path = _try_mount_seestar_smb(host)
        if mount_path:
            _start_image_watcher_at(mount_path)
            return True
    path = iw_cfg.get("watch_path", "")
    if path and os.path.isdir(path):
        _start_image_watcher_at(path)
        return True
    return False


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    global _tel, _cam, _cover, _foc, _manual_disconnect
    _manual_disconnect = True
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
    if _cloud is not None:
        _cloud.request_heartbeat()
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
        logger.exception("Set tracking failed")
        return jsonify({"error": "Unable to set telescope tracking"}), 500
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
                logger.exception("Alt-Az coordinate conversion failed")
                return jsonify({"error": "Unable to slew to the requested position"}), 500
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
            logger.exception("Telescope slew failed")
            return jsonify({"error": "Telescope slew failed"}), 500

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
        logger.exception("Could not read telescope position")
        return jsonify({"error": "Could not read telescope position"}), 500

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
        logger.exception("Nudge slew failed")
        return jsonify({"error": "Nudge slew failed"}), 500

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
        logger.exception("Move-axis command failed")
        return jsonify({"error": "Move-axis command failed"}), 500


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
        logger.exception("Exposure abort failed")
        return jsonify({"error": "Exposure abort failed"}), 500
    return jsonify({"ok": True})


@app.route("/api/cloud")
def api_cloud():
    if _cloud is None:
        return jsonify({"enabled": False})
    return jsonify({"enabled": True, **_cloud.status})


@app.route("/api/cloud/connect", methods=["POST"])
def api_cloud_connect():
    """Re-read config and attempt anonymous registration (no account link).

    Prefer POST /api/cloud/credentials from the signed-in member app.
    """
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


@app.route("/api/cloud/credentials", methods=["POST"])
def api_cloud_credentials():
    """Install cloud credentials from the signed-in desktop app (localhost only)."""
    if request.remote_addr not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"error": "local only"}), 403
    if _cloud is None:
        return jsonify({"ok": False, "error": "cloud communicator not running"}), 503
    body = request.get_json(force=True, silent=True) or {}
    node_id = str(body.get("node_id") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    if not node_id or not api_key:
        return jsonify({"ok": False, "error": "node_id and api_key required"}), 400
    try:
        _cloud.install_credentials(node_id, api_key)
    except Exception as exc:
        logger.exception("install_credentials failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "registered": True, "node_id": node_id})


@app.route("/api/cloud/identity")
def api_cloud_identity():
    """This node's existing cloud credentials, for the local member app.

    A node registers itself anonymously on first boot, so by the time its
    owner links it in the app a cloud node already exists for this computer.
    Without a way to read those credentials the app cannot prove ownership,
    so it registers a *second* node and the first is orphaned — a row nobody
    owns that never observes. Handing the identity to a local caller lets the
    app claim the existing node instead (POST /me/nodes/attach with
    node_id + api_key).

    Localhost only, matching POST /api/cloud/credentials, which is strictly
    more powerful (it can repoint this node at another identity entirely).
    Browser access is separately blocked by the cross-origin guard.
    """
    if request.remote_addr not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"error": "local only"}), 403
    if _cloud is None:
        return jsonify({"registered": False})
    node_id, api_key = _cloud.credentials()
    if not (node_id and api_key):
        return jsonify({"registered": False})
    return jsonify({"registered": True, "node_id": node_id, "api_key": api_key})


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
            "history":     list(_state["photometry"]["history"]),
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
        logger.exception("Writing config.yaml failed")
        return jsonify({"error": "Could not save configuration"}), 500
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
        logger.exception("Writing parsed configuration failed")
        return jsonify({"error": "Could not save configuration"}), 500
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
        logger.exception("Writing horizon mask failed")
        return jsonify({"error": "Could not save horizon mask"}), 500
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


@app.route("/api/imaging/targets", methods=["GET"])
def api_imaging_targets():
    """Objects worth imaging, best first. ?search=&limit=&reachable=1

    Shares _IMAGING_TYPES and _imaging_rank with the automatic handover, so a
    suggested target and a chosen one are drawn from the same set. Filtering
    client-side instead would drift: the browse list would start offering dark
    nebulae the telescope itself would never pick.
    """
    search = (request.args.get("search") or "").strip().lower()
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 200))
    except (TypeError, ValueError):
        limit = 20
    reachable_only = request.args.get("reachable") in ("1", "true", "yes")

    items = [o for o in _dso_catalog if str(o.get("type") or "") in _IMAGING_TYPES]
    if search:
        items = [o for o in items
                 if search in str(o.get("id", "")).lower()
                 or search in str(o.get("name", "")).lower()]
    items = sorted(items, key=_imaging_rank)

    if reachable_only:
        out = []
        for obj in items:
            if len(out) >= limit:
                break
            try:
                if _slew_rejection(float(obj["ra"]), float(obj["dec"])) is None:
                    out.append(obj)
            except (TypeError, ValueError, KeyError):
                continue
        return jsonify({"targets": out, "total": len(items),
                        "reachable_only": True})

    return jsonify({"targets": items[:limit], "total": len(items),
                    "reachable_only": False})


def _keep_registered_loop() -> None:
    """Keep the telescope present in Claude Desktop's config.

    Claude Desktop stores its own settings in that file and rewrites it from
    memory, so an entry added while it is running is dropped again the next
    time it saves. The first real install hit exactly that: the tools were
    registered, Claude overwrote the file, and the member asked to connect
    their telescope and got generic advice from an assistant that had no tools.

    Checking from here costs nothing -- the agent is already running all night
    -- and it repairs the case no amount of install-time care can: the member
    quits Claude an hour later and it takes our entry with it.
    """
    import sys
    from telescope_mcp.register_client import ensure_registered

    # Only the installed agent owns this registration. A source checkout must
    # never touch it: the test suite boots dashboards in temp directories, and
    # each one happily rewrote the member's real Claude config to point at a
    # venv interpreter and a scratch path that is deleted moments later. Claude
    # then reports "Server disconnected", which is exactly what happened.
    if not getattr(sys, "frozen", False):
        logger.debug("Not a packaged build — leaving Claude's config alone")
        return

    data_dir = str(pathlib.Path.cwd())
    while True:
        try:
            ensure_registered(sys.executable, data_dir)
        except Exception as exc:
            logger.debug("Registration check failed: %s", exc)
        time.sleep(300)


def _next_step() -> dict:
    """What this member should do right now, and what happens after that.

    Setting a telescope up is a sequence of handoffs, and each one used to
    assume the person would work out the next move themselves. They do not --
    the person who built this said he barely does. So one place decides, from
    the node's actual state, and the assistant leads with what it says instead
    of waiting to be asked.

    Ordered by what blocks what: no telescope is useless without an account,
    and an account is useless if the telescope was never found.
    """
    from telescope_mcp.register_client import installed_clients

    assistants = []
    try:
        assistants = installed_clients()
    except Exception:
        pass

    linked = False
    node_id = ""
    try:
        if _cloud is not None:
            node_id, key = _cloud.credentials()
            linked = bool(node_id and key)
    except Exception:
        pass

    with _state_lock:
        scope = bool(_state.get("telescope", {}).get("connected"))
        camera = bool(_state.get("camera", {}).get("connected"))

    step = {"assistants": assistants, "linked": linked,
            "telescope_connected": scope, "node_id": node_id}

    if not scope:
        step.update({
            "state": "no_telescope",
            "headline": "Let's find your telescope.",
            "say": "connect my telescope",
            "detail": ("First check it is on your home Wi-Fi — in its own app "
                       "that is called Station Mode. Out of the box most smart "
                       "telescopes make their own network, and while they do "
                       "this computer cannot see them."),
        })
        return step

    if not linked:
        step.update({
            "state": "not_linked",
            "headline": "Your telescope is connected. Now link it to an account.",
            "say": "connect my telescope",
            "detail": ("This opens a browser page where you can sign in or "
                       "make an account. It takes a moment, and then your "
                       "measurements start counting towards the network."),
        })
        return step

    if not camera:
        step.update({
            "state": "no_camera",
            "headline": "The telescope is linked, but its camera has not reported in.",
            "say": "is anything wrong?",
            "detail": "I can read the logs and tell you what is missing.",
        })
        return step

    step.update({
        "state": "ready",
        "headline": "Everything is connected. Your telescope observes on its own tonight.",
        "say": "what's the plan tonight?",
        "detail": ("You do not have to be awake for any of it. Ask in the "
                   "morning to see what it measured and imaged."),
    })
    return step


@app.route("/api/next-step")
def api_next_step():
    """The next thing this member should do, so nothing has to guess."""
    try:
        return jsonify(_next_step())
    except Exception as exc:
        logger.exception("Could not work out the next step")
        # Never leave the page with nothing to say.
        return jsonify({"state": "unknown",
                        "headline": "Let's see where your telescope is up to.",
                        "say": "is anything wrong?",
                        "detail": f"({type(exc).__name__})",
                        "assistants": []})


@app.route("/api/imaging/status", methods=["GET"])
def api_imaging_status():
    """Whether the imaging half of the night is running, and on what."""
    return jsonify(imaging_status())


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


# ── Never-idle: alternates, gap fill, starvation ───────────────────────────────

def _store_alternates(contingencies: dict) -> None:
    """Validate and stash the plan's contingency ladder. Replaced wholesale on
    each new plan; invalid alternates are dropped individually."""
    raw = contingencies.get("alternates") or []
    kept: list[dict] = []
    for alt in raw:
        if not isinstance(alt, dict):
            continue
        valid, err = _validate_schedule_items([alt])
        if err is not None:
            logger.debug("Alternate %s dropped by validator: %s",
                         alt.get("target", "?"), err)
            continue
        item = valid[0]
        item["expected_info"] = float(alt.get("expected_info", 0.0) or 0.0)
        kept.append(item)
    kept.sort(key=lambda a: -a["expected_info"])
    with _alternates_lock:
        _alternates.clear()
        _alternates.extend(kept)
        _alternates_used.clear()
    if kept:
        logger.info("Stored %d plan alternates for local fill", len(kept))


def _estimated_dwell_s(item: dict) -> float:
    """Estimated wall-clock cost of running an item now: exposures plus a
    slew/settle margin. time_series items cost their full window."""
    dur_min = float(item.get("duration_minutes", 0.0) or 0.0)
    if str(item.get("observation_mode", "")) == "time_series" and dur_min > 0:
        return dur_min * 60.0 + _GAP_FILL_MARGIN_S
    exp = float(item.get("expDur", 60) or 60)
    count = int(item.get("expCount", 1) or 1)
    return exp * count + _GAP_FILL_MARGIN_S


def _pick_gap_filler(alternates: list, gap_s: float, used: set) -> Optional[int]:
    """Index of the best unused alternate whose estimated dwell fits inside
    gap_s, or None. Pure — unit-testable without device state."""
    for i, alt in enumerate(alternates):
        if i in used:
            continue
        if _estimated_dwell_s(alt) <= gap_s:
            return i
    return None


def _next_alternate(gap_s: Optional[float] = None) -> Optional[dict]:
    """Pop the best unused alternate (optionally constrained to fit a gap),
    marking it used. Returns a copy safe to mutate."""
    with _alternates_lock:
        if gap_s is None:
            idx = next((i for i in range(len(_alternates))
                        if i not in _alternates_used), None)
        else:
            idx = _pick_gap_filler(_alternates, gap_s, _alternates_used)
        if idx is None:
            return None
        _alternates_used.add(idx)
        return dict(_alternates[idx])


def _is_dark_now() -> bool:
    """True when the sun is below the observing threshold (best-effort)."""
    if _safety_mgr is None:
        return False
    try:
        s = _safety_mgr.status()
    except Exception:
        return False
    sun = s.get("sun_elevation")
    thr = s.get("dawn_threshold", -18.0)
    if not isinstance(sun, (int, float)) or not isinstance(thr, (int, float)):
        return False
    return sun <= thr


def _mark_work_starved() -> None:
    """Plan + alternates exhausted with dark time left: raise the flag the
    heartbeat carries so cloud reflow tops this node up within ~1 tick."""
    if _work_starved.is_set():
        return
    _work_starved.set()
    logger.info("Work-starved: plan and alternates exhausted with dark "
                "time remaining — signalling cloud for top-up")
    _telemetry.event("work_starved", severity="info", detail={})
    if _cloud is not None:
        try:
            _cloud.request_heartbeat()
        except Exception:
            pass


def _start_wait_seconds(start_str: str, now: Optional[time.struct_time] = None) -> float:
    """Seconds to wait before an item's HH:MM local start time.

    Times carry no date, so the delay is interpreted modulo 24 h: anything up
    to 8 h in the past is an overdue item that should run immediately (0);
    everything else is tonight's future and is waited for in full.  (An
    earlier 2 h wait cap made a plan received at dusk execute the whole night
    back-to-back, missing every transit window.)  Returns 0 on parse errors.
    """
    try:
        sh, sm = map(int, start_str.split(":"))
    except (ValueError, AttributeError):
        return 0.0
    if not (0 <= sh < 24 and 0 <= sm < 60):
        return 0.0
    now = now or time.localtime()
    target_s = sh * 3600 + sm * 60
    now_s    = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    wait_s   = float(target_s - now_s)
    if wait_s < 0:
        wait_s += 86400.0
    if wait_s >= 16 * 3600:
        return 0.0  # started < 8 h ago — overdue, run now
    return wait_s


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
    starts_at_utc = str(item.get("starts_at_utc") or "")
    latest_start_utc = str(item.get("latest_start_utc") or "")
    obs_mode  = item.get("observation_mode", "single_epoch")
    duration_minutes = float(item.get("duration_minutes", 0.0))

    with _sched_lock:
        _sched_state.update({
            "current_idx": idx,
            "current_target": target,
            "current_phase": "waiting",
            "current_frame": 0,
            "total_frames": exp_count,
            "current_item_id": str(item.get("item_id") or ""),
            "current_bundle_id": str(item.get("bundle_id") or ""),
            "current_filter": str(item.get("filter") or ""),
            "current_task_id": str(item.get("task_id") or ""),
            "cancel_after_frame": False,
            "current_item_outcome": "started",
            "current_failure_reason": "",
        })

    # Upgraded agents use absolute UTC. Legacy HH:MM remains a fallback.
    wait_s = 0.0
    if starts_at_utc:
        try:
            start_dt = datetime.fromisoformat(starts_at_utc.replace("Z", "+00:00"))
            wait_s = max(0.0, (start_dt - datetime.now(timezone.utc)).total_seconds())
        except ValueError:
            logger.warning("Schedule: invalid starts_at_utc for %s", target)
    elif start_str:
        wait_s = _start_wait_seconds(start_str)
    if wait_s > 0:
        logger.info("Schedule: waiting %.0f s for %s", wait_s, target)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and not _sched_cancelled():
            time.sleep(1)

    if latest_start_utc:
        try:
            latest = datetime.fromisoformat(latest_start_utc.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > latest:
                raise RuntimeError("latest start window elapsed")
        except ValueError:
            raise RuntimeError("invalid latest_start_utc")

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
                _sched_state["current_item_outcome"] = "skipped"
                _sched_state["current_failure_reason"] = rejection
            _telemetry.event("slew_rejected", severity="warning", target=target,
                             detail={"reason": rejection})
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
                    _sched_state["current_item_outcome"] = "failed"
                    _sched_state["current_failure_reason"] = "slew timeout"
                _telemetry.event("slew_failed", severity="error", target=target,
                                 detail={"reason": "timeout", "timeout_s": 180})
        except Exception as exc:
            logger.error("Schedule: slew failed for %s: %s", target, exc)
            with _sched_lock:
                _sched_state["error"] = f"Slew to {target} failed: {exc}"
                _sched_state["current_item_outcome"] = "failed"
                _sched_state["current_failure_reason"] = str(exc)[:500]
            _telemetry.event("slew_failed", severity="error", target=target,
                             detail={"reason": str(exc)[:300]})
    else:
        logger.warning("Schedule: telescope not connected — skipping slew for %s", target)
        _telemetry.event("device_disconnect", severity="error", target=target,
                         detail={"reason": "telescope not connected during schedule"})
        with _sched_lock:
            _sched_state["current_item_outcome"] = "failed"
            _sched_state["current_failure_reason"] = "telescope disconnected"

    if _sched_cancelled():
        return

    # Don't expose if the slew didn't confirm a settled, on-target mount.
    if _tel is not None and not slew_ok:
        return

    # ── Expose ────────────────────────────────────────────────────────────────
    def _take_frame(frame: int, total: int) -> bool:
        """Take one exposure. Returns False if cancelled, True otherwise."""
        if _sched_cancelled():
            return False
        with _sched_lock:
            _sched_state["current_phase"] = "exposing"
            _sched_state["current_frame"] = frame
            _sched_state["total_frames"]  = total

        logger.info("Schedule: %s frame %d/%d (%.1fs bin%d)",
                    target, frame, total, exp_dur, binning)

        if _cam is None:
            logger.warning("Schedule: camera not connected — skipping exposure for %s", target)
            with _sched_lock:
                _sched_state["current_item_outcome"] = "failed"
                _sched_state["current_failure_reason"] = "camera disconnected"
            return False

        fits_save_path: Optional[str] = None
        with _state_lock:
            phot_enabled = _state["photometry"]["enabled"]
        if phot_enabled:
            safe_tgt = "".join(
                c if c.isalnum() or c in "-_ " else "_" for c in target
            ).strip()
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
                b64 = _capture_image(fits_path=fits_save_path, exp_dur=exp_dur, target=target)
            if b64:
                _store_history_image(target, exp_dur, binning, frame, total, b64)
            if fits_save_path and pathlib.Path(fits_save_path).exists():
                _enqueue_photometry(fits_save_path)
        except ExposureCancelled:
            logger.warning("Schedule: frame %d of %s aborted", frame, target)
            if _sched_cancelled():
                return False
        except Exception as exc:
            logger.error("Schedule: exposure failed %s frame %d: %s", target, frame, exc)
            _telemetry.event("exposure_failed", severity="error", target=target,
                             detail={"frame": frame, "reason": str(exc)[:300]})
            with _sched_lock:
                _sched_state["current_item_outcome"] = "failed"
                _sched_state["current_failure_reason"] = str(exc)[:500]
        finally:
            _pier_cam_pause.clear()
        with _sched_lock:
            if _sched_state.get("cancel_after_frame"):
                _sched_state["cancelled"] = True
                _sched_state["current_item_outcome"] = "cancelled"
                return False
        return True

    if obs_mode == "time_series" and duration_minutes > 0:
        # Continuous monitoring for the full transit/time-series window.
        deadline = time.monotonic() + duration_minutes * 60.0
        estimated_total = max(exp_count, int(duration_minutes * 60.0 / max(exp_dur, 1)))
        frame = 0
        logger.info("Schedule: time-series on %s for %.0f min (~%d frames)",
                    target, duration_minutes, estimated_total)
        with _sched_lock:
            _sched_state["current_phase"] = "exposing"
            _sched_state["total_frames"]  = estimated_total
        while time.monotonic() < deadline and not _sched_cancelled():
            frame += 1
            remaining_s = deadline - time.monotonic()
            if remaining_s < exp_dur * 0.5:
                break  # not enough time for a useful frame
            if not _take_frame(frame, estimated_total):
                return
    else:
        for frame in range(1, exp_count + 1):
            if not _take_frame(frame, exp_count):
                return


def _wait_for_darkness(max_wait_s: float = 16 * 3600) -> bool:
    """Block until the sun is below the dawn threshold (i.e. observing dark).

    Cloud plans can arrive at any time of day (the cloud replans every couple
    of hours); starting them immediately used to burn every item against the
    daytime safety latch in seconds, consuming the plan before nightfall.

    Returns True when it's dark (or darkness can't be determined), False when
    cancelled or the wait timed out.
    """
    if _safety_mgr is None:
        return True
    deadline = time.monotonic() + max_wait_s
    announced = False
    while time.monotonic() < deadline:
        if _sched_cancelled():
            return False
        try:
            status = _safety_mgr.status()
        except Exception:
            return True
        if status.get("dry_run"):
            return True  # admin dry-run testing mode — ignore actual sun position
        sun = status.get("sun_elevation")
        threshold = status.get("dawn_threshold", -18.0)
        if sun is None:
            return True  # no location configured — can't gate on darkness
        dawn_latched = (not status.get("safe", True)
                        and str(status.get("reason", "")).startswith("dawn"))
        if sun <= threshold and not dawn_latched:
            return True
        if not announced:
            announced = True
            logger.info("Schedule: sun at %.1f° (threshold %.1f°) — waiting for dark",
                        sun, threshold)
            with _sched_lock:
                _sched_state["current_phase"] = "waiting_for_dark"
        # Sleep ~30 s between sun checks, but stay responsive to cancellation
        # (a superseding cloud plan cancels a waiting schedule and expects it
        # to unwind within seconds).
        for _ in range(30):
            if _sched_cancelled():
                return False
            time.sleep(1)
    logger.warning("Schedule: darkness wait timed out after %.0f h", max_wait_s / 3600)
    return False


def _run_schedule_bg(items: list, source: str = "manual",
                     wait_for_dark: bool = False) -> None:
    """Background thread: slew + expose for each scheduled observation."""
    with _sched_lock:
        _sched_state.update({
            "running": True, "cancelled": False,
            "current_idx": -1, "current_target": "",
            "current_phase": "starting",
            "current_frame": 0, "total_frames": 0,
            "completed": 0, "total": len(items), "error": None,
            "source": source,
            "started_at": time.time(),
            "items": [
                {
                    "target":    it.get("target", "Unknown"),
                    "ra":        it.get("ra"),
                    "dec":       it.get("dec"),
                    "expDur":    it.get("expDur"),
                    "expCount":  it.get("expCount"),
                    "startTime": it.get("startTime", ""),
                    "filter":    it.get("filter", ""),
                    "item_id":   it.get("item_id", ""),
                    "bundle_id": it.get("bundle_id", ""),
                }
                for it in items
            ],
        })
    imaging_handoff = False
    logger.info("Schedule started: %d observations", len(items))
    _telemetry.event("schedule_started", severity="info",
                     detail={"items": len(items), "source": source,
                             "wait_for_dark": wait_for_dark})

    if wait_for_dark and not _wait_for_darkness():
        with _sched_lock:
            cancelled = _sched_state["cancelled"]
            _sched_state["running"] = False
            _sched_state["current_phase"] = "cancelled" if cancelled else "done"
        _telemetry.event("schedule_abandoned_before_dark",
                         severity="info" if cancelled else "warning",
                         detail={"cancelled": cancelled, "source": source})
        return

    # Autonomous runs that start right after a service restart can beat the
    # supervisor's device reconnect — give it a couple of minutes rather than
    # burning every item against a not-yet-connected telescope.
    if source in ("cloud", "interrupt") and _tel is None and items:
        logger.info("Schedule: telescope not connected yet — waiting up to 180 s")
        deadline = time.monotonic() + 180
        while _tel is None and time.monotonic() < deadline and not _sched_cancelled():
            time.sleep(2)
        if _tel is None and not _sched_cancelled():
            _telemetry.event(
                "device_disconnect", severity="error",
                detail={"reason": "telescope never connected before schedule start",
                        "waited_s": 180})

    _sched_prepare_mount()

    gap_fill_min_s = 900.0
    try:
        gap_fill_min_s = float(_load_config().get("cloud", {})
                               .get("gap_fill_min_s", 900))
    except Exception:
        pass

    try:
        queued_item_ids = {str(i.get("item_id") or "") for i in items}
        for idx, item in enumerate(items):
            if _sched_cancelled():
                break
            # Tonight's intent is re-checked per item, not just at the start:
            # a member can stand the telescope down mid-night, and the weather
            # can close in after the run began.
            if not _tonight_allows_observing():
                logger.info("Schedule: stopping — tonight's intent says stop (%s)",
                            _tonight_intent().get("reason", ""))
                break
            # A bounded research block hands the rest of the night to imaging.
            if _research_window_expired():
                logger.info("Schedule: research block complete after %.1f h — "
                            "remaining items released for imaging",
                            float((_tonight_intent().get("proposal") or {})
                                  .get("research_hours") or 0.0))
                _telemetry.event("research_window_complete", severity="info",
                                 detail={"completed": idx, "total": len(items)})
                # Hand over rather than simply stopping. Without this the night
                # ended here: the log claimed the rest was "released for
                # imaging" and nothing was listening, so a member who asked for
                # two hours of research and then a picture got the research and
                # a parked telescope.
                imaging_handoff = True
                break
            # ── Gap fill: a dead wait before this item's start time is spent
            # on an alternate that fits, instead of sleeping (cloud plans only;
            # never shave the margin before a time-series window).
            # Offline work has a separate signed contingency list and may not
            # select ordinary local alternates.
            if source == "cloud" and not item.get("bundle_id"):
                while not _sched_cancelled():
                    wait_s = _start_wait_seconds(item.get("startTime", ""))
                    if wait_s < gap_fill_min_s:
                        break
                    filler = _next_alternate(gap_s=wait_s - _GAP_FILL_MARGIN_S)
                    if filler is None:
                        break
                    filler["startTime"] = ""   # run now — we ARE the gap
                    logger.info("Schedule: filling %.0f s gap before %s with "
                                "alternate %s", wait_s,
                                item.get("target", "?"),
                                filler.get("target", "?"))
                    try:
                        _run_schedule_observation(idx, filler)
                    except Exception as exc:
                        logger.error("Schedule: gap-fill %s failed: %s",
                                     filler.get("target", "?"), exc)
            if _sched_cancelled():
                break
            if (item.get("bundle_id") and _cloud is not None
                    and getattr(_cloud, "outbox_at_capacity", lambda: False)()):
                _cloud.record_execution_outcome(
                    str(item.get("bundle_id")), str(item.get("item_id") or ""), "skipped",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    failure_reason="offline outbox storage budget exhausted",
                    detail={"offline": True})
                _telemetry.event("offline_storage_exhausted", severity="critical",
                                 detail={"item_id": item.get("item_id", "")})
                break
            attempt_id = None
            offline_execution = bool(_cloud is not None
                                     and _cloud.status.get("last_heartbeat_ok") is False)
            if _cloud is not None and item.get("item_id"):
                attempt_id = _cloud.record_execution_outcome(
                    str(item.get("bundle_id") or "connected-plan"),
                    str(item.get("item_id")), "started",
                    started_at=datetime.now(timezone.utc).isoformat(),
                    last_checkpoint="item_started",
                    task_id=str(item.get("task_id") or ""),
                    frames_attempted=0, frames_completed=0,
                    detail={"offline": offline_execution})
            try:
                _run_schedule_observation(idx, item)
                with _sched_lock:
                    item_outcome = str(_sched_state.get("current_item_outcome") or "started")
                    item_reason = str(_sched_state.get("current_failure_reason") or "")
                state = ("cancelled" if _sched_cancelled()
                         else "completed" if item_outcome == "started" else item_outcome)
                if _cloud is not None and item.get("item_id"):
                    _cloud.record_execution_outcome(
                        str(item.get("bundle_id") or "connected-plan"),
                        str(item.get("item_id")), state, attempt_id=attempt_id,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        task_id=str(item.get("task_id") or ""),
                        frames_attempted=int(_sched_state.get("current_frame") or 0),
                        frames_completed=int(_sched_state.get("current_frame") or 0),
                        last_checkpoint="item_finished",
                        failure_reason=item_reason,
                        detail={"offline": offline_execution})
                if (state in ("failed", "skipped") and _cloud is not None
                        and item.get("bundle_id")):
                    for alternate in _cloud.autonomy_contingency_items():
                        alt_id = str(alternate.get("item_id") or "")
                        if alt_id and alt_id not in queued_item_ids:
                            valid_alt, alt_error = _validate_schedule_items([alternate])
                            if alt_error is None:
                                items.extend(valid_alt)
                                queued_item_ids.add(alt_id)
                                with _sched_lock:
                                    _sched_state["total"] = len(items)
            except Exception as exc:
                # A single bad observation must not abort the whole night.
                logger.error("Schedule: observation %d (%s) failed: %s",
                             idx, item.get("target", "?"), exc)
                with _sched_lock:
                    _sched_state["error"] = str(exc)
                if _cloud is not None and item.get("item_id"):
                    _cloud.record_execution_outcome(
                        str(item.get("bundle_id") or "connected-plan"),
                        str(item.get("item_id")), "failed", attempt_id=attempt_id,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        task_id=str(item.get("task_id") or ""),
                        failure_reason=str(exc)[:500],
                        detail={"offline": offline_execution})
            with _sched_lock:
                _sched_state["completed"] = idx + 1
            logger.info("Schedule: ✓ %s (%d/%d)",
                        item.get("target", "?"), idx + 1, len(items))

        # ── End-of-plan fill: the plan is exhausted but the night isn't.
        # Run remaining alternates (ignoring their start times) until dawn,
        # cancellation, or exhaustion; then signal work starvation so cloud
        # reflow tops us up.
        if source == "cloud" and not _sched_cancelled():
            fill_idx = len(items)
            while not _sched_cancelled() and _is_dark_now():
                alt = _next_alternate()
                if alt is None:
                    _mark_work_starved()
                    break
                alt["startTime"] = ""   # run now — leftover dark time
                logger.info("Schedule: plan exhausted, dark time left — "
                            "running alternate %s", alt.get("target", "?"))
                try:
                    _run_schedule_observation(fill_idx, alt)
                except Exception as exc:
                    logger.error("Schedule: end-of-plan alternate %s failed: %s",
                                 alt.get("target", "?"), exc)
                fill_idx += 1
    except Exception as exc:
        logger.error("Schedule crashed: %s", exc)
        with _sched_lock:
            _sched_state["error"] = str(exc)
        _telemetry.event("schedule_crashed", severity="error",
                         detail={"error": str(exc)[:300]})
    finally:
        with _sched_lock:
            _sched_state["running"] = False
            _sched_state["current_phase"] = (
                "cancelled" if _sched_state["cancelled"] else "done"
            )
            completed = _sched_state["completed"]
            error = _sched_state["error"]
            cancelled = _sched_state["cancelled"]
        logger.info("Schedule finished")
        _telemetry.event(
            "schedule_finished", severity="info",
            detail={"completed": completed, "total": len(items),
                    "cancelled": cancelled, "error": error, "source": source})

    # The imaging half of the night. Outside the finally block so the schedule
    # is properly marked done first -- imaging is a separate activity, not a
    # continuation of the run, and reporting the research as still in progress
    # while stacking would misdescribe what the telescope is doing.
    if imaging_handoff and not cancelled and _tonight_allows_observing():
        try:
            _run_imaging_block()
        except Exception as exc:
            logger.warning("Imaging block failed: %s", exc)
            _telemetry.event("imaging_failed", severity="warning",
                             detail={"error": str(exc)[:200]})


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
        # Time-series fields: the executor reads these to hold a target for a
        # transit window. They must survive validation or every cloud
        # time-series plan silently degrades to a single-epoch visit.
        obs_mode = str(item.get("observation_mode", "single_epoch") or "single_epoch")
        if obs_mode not in ("single_epoch", "time_series"):
            return [], f"item '{label}': unknown observation_mode '{obs_mode}'"
        try:
            duration_minutes = float(item.get("duration_minutes", 0.0) or 0.0)
        except (TypeError, ValueError):
            return [], f"item '{label}': duration_minutes must be numeric"
        if duration_minutes < 0 or duration_minutes > 12 * 60:
            return [], f"item '{label}': duration_minutes must be in [0, 720]"
        out.append({
            "target": str(item.get("target", "Unknown")),
            "ra": ra, "dec": dec, "expDur": exp_dur,
            "expCount": exp_count, "binning": binning,
            "startTime": str(item.get("startTime", "")),
            "observation_mode": obs_mode,
            "duration_minutes": duration_minutes,
            "filter": str(item.get("filter", "") or ""),
            "notes": str(item.get("notes", "") or ""),
            "item_id": str(item.get("item_id", "") or ""),
            "task_id": str(item.get("task_id", "") or ""),
            "bundle_id": str(item.get("bundle_id", "") or ""),
            "starts_at_utc": str(item.get("starts_at_utc", "") or ""),
            "latest_start_utc": str(item.get("latest_start_utc", "") or ""),
            "task_type": str(item.get("task_type", "science") or "science"),
            "campaign_id": str(item.get("campaign_id", "") or ""),
            "priority": float(item.get("priority", 0.0) or 0.0),
            "cancellation_generation": int(
                item.get("cancellation_generation", 0) or 0),
        })
    return out, None


@app.route("/api/events", methods=["GET"])
def api_events():
    """Structured reliability events (newest last). ?source=disk reads the
    persisted JSONL (survives restarts); default is the in-memory ring."""
    n = min(int(request.args.get("n", 100)), 1000)
    min_severity = request.args.get("min_severity", "debug")
    if request.args.get("source") == "disk":
        events = _telemetry.load_events_file(n)
    else:
        events = _telemetry.recent(n, min_severity=min_severity)
    return jsonify({"events": events, "counters": _telemetry.counters()})


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
    # Image identifiers are generated locally.  Reject path syntax before an
    # identifier is ever used to construct the on-disk cache filename.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", img_id):
        return jsonify({"error": "Image not found"}), 404
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
        logger.exception("Location search failed")
        return jsonify({"error": "Location search failed"}), 500


# ── Entry point ────────────────────────────────────────────────────────────────

def launch(port: int = 5173) -> None:
    global _safety_mgr, _image_watcher, _cloud, _commissioning

    import urllib.request

    # Refuse to start a second instance. Flask's app.run() runs in a daemon
    # thread below — if the port is already taken, that bind failure kills
    # the thread silently while the rest of launch() (supervisor, safety
    # manager, cloud comms) keeps going, producing a second process that
    # fights the real instance for the same Alpaca connection and leaves it
    # stuck reporting "can't reach the telescope" even though the server
    # is fine. Fail loudly here instead, before any device connections open.
    import socket as _socket
    try:
        _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        _probe.bind(("127.0.0.1", port))
        _probe.close()
    except OSError:
        print(
            f"\n  NODE v1 is already running on port {port}.\n"
            f"  Refusing to start a second instance — running two at once causes "
            f"duplicate connections to your telescope and can leave the dashboard "
            f"stuck reporting it can't connect.\n"
            f"  Quit the other instance first.\n",
            file=sys.__stderr__,
        )
        sys.exit(1)

    cfg = _load_config()
    log_cfg = cfg.get("logging", {})
    log_fmt = log_cfg.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handlers: list = [logging.StreamHandler()]
    try:
        from logging.handlers import RotatingFileHandler
        import os
        os.makedirs("logs", exist_ok=True)
        file_handler = RotatingFileHandler(
            "logs/node.log", maxBytes=10 * 1024 * 1024, backupCount=5,
        )
        file_handler.setFormatter(logging.Formatter(log_fmt))
        handlers.append(file_handler)
    except Exception as exc:
        print(f"Could not set up file logging: {exc}")
    logging.basicConfig(
        level=log_cfg.get("level", "INFO"),
        format=log_fmt,
        handlers=handlers,
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
        configured_path = iw_cfg.get("watch_path", "")
        # The configured path is a Linux CIFS default; on macOS the Seestar
        # share always mounts at /Volumes/Seestar (_try_mount_seestar_smb), so
        # an unmodified Linux-style default would never exist on this platform.
        if configured_path and os.path.isdir(configured_path):
            watch_path = configured_path
        elif platform.system() == "Darwin":
            watch_path = "/Volumes/Seestar"
        else:
            watch_path = configured_path or "/mnt/seestar"
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
            on_interrupt=_on_cloud_interrupt,
            on_task_cancel=_on_cloud_task_cancel,
            get_telescope_specs=_cloud_telescope_specs,
            get_state=_cloud_state,
            on_location=_on_cloud_location,
            on_dry_run=_on_cloud_dry_run,
            on_tonight=_on_cloud_tonight,
        )
        _cloud.start()
        threading.Thread(
            target=_interrupt_dispatcher_loop,
            daemon=True,
            name="interrupt-dispatcher",
        ).start()
        threading.Thread(
            target=_cloud_disconnect_monitor_loop,
            daemon=True,
            name="cloud-disco-monitor",
        ).start()
        threading.Thread(
            target=_offline_autonomy_resume_loop,
            daemon=True,
            name="offline-autonomy-resume",
        ).start()

    # Outside the cloud block: a telescope with no account still needs to be
    # reachable from Claude, and this is the only thing that repairs a config
    # Claude Desktop has overwritten.
    threading.Thread(
        target=_keep_registered_loop,
        daemon=True,
        name="keep-registered",
    ).start()

    def _commission_runtime() -> dict:
        with _state_lock:
            return {
                "telescope_connected": _state["telescope"].get("connected", False),
                "camera_connected": _state["camera"].get("connected", False),
                "image_watcher": dict(_state["image_watcher"]),
            }

    _commissioning = CommissioningManager(
        load_config=_load_config,
        is_registered=lambda: bool(_cloud and _cloud.status.get("registered")),
        runtime_status=_commission_runtime,
        telescope_specs=_cloud_telescope_specs,
        interval_s=float(cfg.get("commissioning", {}).get("interval_s", 10)),
    )
    if cfg.get("commissioning", {}).get("enabled", True):
        _commissioning.start()

    pc_cfg = cfg.get("pier_cam", {})
    if pc_cfg.get("enabled", False):
        _pier_cam_stop.clear()
        threading.Thread(target=_pier_cam_loop, daemon=True, name="pier-cam").start()
        with _state_lock:
            _state["pier_cam"]["enabled"] = True

    # Forward error/critical telemetry to the cloud incident API (best-effort,
    # in a background thread) so failures are diagnosable remotely.
    if _cloud is not None:
        _telemetry.set_forwarder(_cloud.submit_incident)
    _telemetry.event("node_started", severity="info",
                     detail={"version": "node-v1", "port": port,
                             "cloud_enabled": bool(cloud_cfg.get("enabled", False))})

    # Supervisor: headless device reconnect, image-watcher revival, disk
    # health/retention, host-sleep detection.
    _supervisor = NodeSupervisor(
        load_config=_load_config,
        devices_connected=_supervisor_devices_ok,
        connect_default=_supervisor_connect,
        watcher_ok=_supervisor_watcher_ok,
        restart_watcher=_revive_image_watcher,
    )
    _supervisor.start()

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
            # Local readiness probe only.
            urllib.request.urlopen(f"{url}/api/status", timeout=0.5)  # nosec B310
            break
        except Exception:
            time.sleep(0.25)

    print(f"\n  NODE v1 running at {url}\n", file=sys.__stdout__)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down.", file=sys.__stdout__)
    finally:
        _pier_cam_stop.set()
        if _cloud is not None:
            _cloud.stop()
        if _commissioning is not None:
            _commissioning.stop()
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
