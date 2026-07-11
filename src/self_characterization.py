"""
Self-characterization — the rig measures its own optics.

Every plate-solved frame yields ground truth no spec sheet can match:

    pixel scale  — sqrt(|det CD|) · 3600 from the solved WCS
    field of view — pixel scale × frame diagonal
    FWHM (arcsec) — the pipeline's pixel FWHM × measured scale
    limiting mag  — faintest catalog-matched survey source at SNR ≥ 5

run_pipeline / run_survey_pipeline call record_frame() after each solve;
rolling medians accumulate in data/characterization.json. Every
REPORT_EVERY solved frames maybe_report() hands back a payload for
POST /api/v1/nodes/characterization, where the cloud (guardedly) updates the
registry columns CHORUS already consumes — so a NINA/EKOS/ASIAIR rig that
registered with vague specs converges to its true capability within a night.
"""

import json
import logging
import os
import statistics
import threading
from typing import Optional

logger = logging.getLogger("self_characterization")

_STATE_FILE = os.path.join("data", "characterization.json")
_MAX_SAMPLES = 200          # rolling window per metric
REPORT_EVERY = 25           # solved frames between cloud reports

_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(_STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"samples": {}, "n_frames": 0, "frames_since_report": 0}


def _save(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, _STATE_FILE)
    except OSError as exc:
        logger.debug("Could not persist characterization state: %s", exc)


def record_frame(
    pixel_scale_arcsec: Optional[float] = None,
    fov_deg: Optional[float] = None,
    fwhm_arcsec: Optional[float] = None,
    limiting_mag: Optional[float] = None,
) -> None:
    """Fold one solved frame's measurements into the rolling windows."""
    metrics = {
        "pixel_scale_arcsec": pixel_scale_arcsec,
        "fov_deg": fov_deg,
        "fwhm_arcsec": fwhm_arcsec,
        "limiting_mag": limiting_mag,
    }
    with _lock:
        state = _load()
        samples = state.setdefault("samples", {})
        for key, val in metrics.items():
            if val is None or not (val == val) or val <= 0:   # NaN/invalid
                continue
            arr = samples.setdefault(key, [])
            arr.append(round(float(val), 4))
            if len(arr) > _MAX_SAMPLES:
                del arr[:-_MAX_SAMPLES]
        state["n_frames"] = int(state.get("n_frames", 0)) + 1
        state["frames_since_report"] = int(state.get("frames_since_report", 0)) + 1
        _save(state)


def maybe_report(force: bool = False) -> Optional[dict]:
    """A characterization payload when one is due (every REPORT_EVERY solved
    frames, or forced right after registration), else None."""
    with _lock:
        state = _load()
        due = force or int(state.get("frames_since_report", 0)) >= REPORT_EVERY
        if not due:
            return None
        samples = state.get("samples") or {}
        payload = {"n_frames": int(state.get("n_frames", 0)),
                   "window_frames": _MAX_SAMPLES}
        have_any = False
        for key in ("pixel_scale_arcsec", "fov_deg", "fwhm_arcsec",
                    "limiting_mag"):
            arr = samples.get(key) or []
            if arr:
                payload[key] = round(statistics.median(arr), 4)
                have_any = True
        if not have_any:
            return None
        state["frames_since_report"] = 0
        _save(state)
    return payload
