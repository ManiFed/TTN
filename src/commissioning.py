"""Automatic, persistent scientific commissioning for newly registered nodes."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from astropy.io import fits

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommissioningManager:
    """Evaluate a node after registration and refine it from the first FITS.

    Checks are intentionally non-invasive: commissioning never slews, opens a
    cover, or exposes a camera by itself. Hardware connection and ordinary
    observing provide the live evidence; the manager resumes across restarts.
    """

    def __init__(
        self,
        *,
        load_config: Callable[[], dict],
        is_registered: Callable[[], bool],
        runtime_status: Callable[[], dict],
        telescope_specs: Callable[[], dict],
        state_path: str = "data/commissioning.json",
        interval_s: float = 10.0,
    ):
        self._load_config = load_config
        self._is_registered = is_registered
        self._runtime_status = runtime_status
        self._telescope_specs = telescope_specs
        self._path = Path(state_path)
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state = self._load()

    def _load(self) -> dict:
        try:
            state = json.loads(self._path.read_text())
            if state.get("schema_version") == SCHEMA_VERSION:
                return state
        except Exception:
            pass
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "waiting_for_signup",
            "started_at": None,
            "completed_at": None,
            "updated_at": _utc_now(),
            "checks": {},
            "capabilities": {},
            "evidence": [],
            "certification": "uncommissioned",
        }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True) + "\n")
        tmp.replace(self._path)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="commissioning"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def restart(self) -> None:
        with self._lock:
            self._state.update({
                "status": "evaluating",
                "started_at": _utc_now(),
                "completed_at": None,
                "checks": {},
                "capabilities": {},
                "evidence": [],
                "certification": "uncommissioned",
            })
            self._save()

    @staticmethod
    def _check(ok: bool, detail: str, *, blocking: bool = True) -> dict:
        return {"ok": bool(ok), "blocking": blocking, "detail": detail}

    def evaluate(self) -> dict:
        cfg = self._load_config() or {}
        # A node running without the cloud is standalone, not half-configured.
        # Waiting for a signup that is never coming would leave it reporting
        # "pending" for ever, and commissioning is what tells an owner their
        # telescope is actually ready to work.
        standalone = not bool((cfg.get("cloud") or {}).get("enabled", True))
        registered = standalone or bool(self._is_registered())
        with self._lock:
            if not registered:
                self._state["status"] = "waiting_for_signup"
                self._state["updated_at"] = _utc_now()
                return self.status_unlocked()
            if self._state["started_at"] is None:
                self._state["started_at"] = _utc_now()

        runtime = self._runtime_status() or {}
        observer = cfg.get("safety", {}).get("observer", {}) or {}
        # Prefer the live, auto-mounted watch path the image watcher actually
        # resolved at runtime over the static config value, which is often a
        # placeholder (e.g. /mnt/seestar) left unset after macOS auto-mounts
        # the Seestar's SMB share under /Volumes/... instead.
        watch_path = Path(str(
            runtime.get("image_watcher", {}).get("watch_path")
            or cfg.get("image_watcher", {}).get("watch_path", "")
        )).expanduser()
        phot_cfg = cfg.get("photometry", {})
        solver_type = str(phot_cfg.get("solver", "astap")).strip().lower()
        solver = str(
            phot_cfg.get("solve_field_path", "solve-field")
            if solver_type == "astrometry"
            else phot_cfg.get("astap_path", "astap")
        )
        solver_ok = Path(solver).expanduser().is_file() or shutil.which(solver) is not None
        free_gb = shutil.disk_usage(Path.cwd()).free / (1024 ** 3)
        specs = self._telescope_specs() or {}

        checks = {
            "cloud_registration": self._check(
                True,
                "Running standalone (no cloud)" if standalone
                else "Node linked to member",
                blocking=not standalone,
            ),
            "observer_location": self._check(
                not (observer.get("latitude") in (None, 0, 0.0)
                     and observer.get("longitude") in (None, 0, 0.0)),
                "Observer coordinates configured",
            ),
            "free_disk": self._check(free_gb >= 5.0, f"{free_gb:.1f} GiB free"),
            "plate_solver": self._check(solver_ok, solver),
            "telescope": self._check(
                bool(runtime.get("telescope_connected")), "Live telescope connection"
            ),
            "camera": self._check(
                bool(runtime.get("camera_connected")), "Live camera connection"
            ),
            "image_ingest": self._check(
                watch_path.is_dir(), str(watch_path)
            ),
            "credential_store": self._check(
                bool(runtime.get("credential_store_ok", True)),
                "System keychain rejected cloud credentials this session — "
                "a restart will orphan this node's history instead of "
                "self-healing" if not runtime.get("credential_store_ok", True)
                else "Cloud credentials persisted to system keychain",
                blocking=False,
            ),
            "science_frame": self._check(
                bool(self._state.get("evidence")), "Waiting for first science FITS",
                blocking=False,
            ),
        }
        blocking_failed = [v for v in checks.values() if v["blocking"] and not v["ok"]]
        certification = "operational" if not blocking_failed else "pending"
        status = "complete" if certification == "operational" else "evaluating"
        with self._lock:
            self._state.update({
                "status": status,
                "certification": certification,
                "checks": checks,
                "capabilities": {**self._state.get("capabilities", {}), **specs},
                "updated_at": _utc_now(),
                "completed_at": _utc_now() if status == "complete" else None,
            })
            self._save()
            return self.status_unlocked()

    def observe_fits(self, fits_path: str) -> None:
        """Add passive scientific evidence from a newly observed FITS file."""
        path = Path(fits_path)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with fits.open(path, memmap=False) as hdul:
                header = hdul[0].header
                data = np.asarray(hdul[0].data, dtype=float)
            finite = data[np.isfinite(data)]
            if finite.size == 0:
                return
            peak = float(np.max(finite))
            p999 = float(np.percentile(finite, 99.9))
            evidence = {
                "file": path.name,
                "sha256": digest,
                "observed_at": _utc_now(),
                "date_obs": header.get("DATE-OBS"),
                "exptime_s": header.get("EXPTIME", header.get("EXPOSURE")),
                "has_wcs": bool(header.get("CTYPE1") and header.get("CTYPE2")),
                "shape": list(data.shape),
                "peak_adu": round(peak, 2),
                "p99_9_adu": round(p999, 2),
            }
            with self._lock:
                existing = self._state.setdefault("evidence", [])
                if not any(item.get("sha256") == digest for item in existing):
                    existing.append(evidence)
                    del existing[:-20]
                caps = self._state.setdefault("capabilities", {})
                caps.update({
                    "science_frame_shape": list(data.shape),
                    "science_frame_peak_adu": round(peak, 2),
                    "fits_timing_keywords": bool(
                        header.get("DATE-OBS")
                        and header.get("EXPTIME", header.get("EXPOSURE")) is not None
                    ),
                    "fits_wcs_present": evidence["has_wcs"],
                })
                self._save()
            self.evaluate()
        except Exception:
            return

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.evaluate()
            except Exception:
                pass
            self._stop.wait(self._interval_s)

    def status_unlocked(self) -> dict:
        return json.loads(json.dumps(self._state))
