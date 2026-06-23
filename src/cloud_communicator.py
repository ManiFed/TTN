#!/usr/bin/env python3
"""
CloudCommunicator — connects this node to the The Telescope Net cloud.

Reads config["cloud"]:
    cloud:
      enabled: true
      url: https://cloud.example.org
      node_id: ''            # blank = auto-register on first start
      api_key: ''            # blank = auto-register on first start
      heartbeat_interval: 60
      plan_poll_interval: 300
      auto_run_plans: false  # hand new plans to the schedule runner automatically
      upload_images: false   # also upload raw FITS after photometry

Behaviour:
    • registers automatically when no credentials exist (persisted to
      data/cloud_state.json so re-registration never repeats)
    • sends heartbeats with optional local conditions from a callback
    • polls for the current observation plan; when the plan_id changes,
      invokes on_plan(items) with node-schedule-format items
    • polls interrupts; invokes on_interrupt(item) for unacked ones, then acks
    • submit_measurement() uploads each photometry result immediately;
      failures queue to disk and retry on the heartbeat cadence

Fully optional: when cloud.enabled is false (default) nothing starts and the
node behaves exactly as before.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from src.shared_models import expand_env

logger = logging.getLogger("cloud_communicator")

def _utc_offset_hours() -> float:
    """Local UTC offset in hours, DST-aware.

    time.timezone is the *standard*-time offset; when DST is in effect the
    actual offset is time.altzone.  Using altzone avoids assuming every DST
    shift is exactly one hour (it isn't, e.g. Lord Howe Island = 30 min).
    """
    if time.daylight and time.localtime().tm_isdst > 0:
        return -time.altzone / 3600.0
    return -time.timezone / 3600.0


_STATE_FILE = Path("data") / "cloud_state.json"
_QUEUE_FILE = Path("data") / "cloud_upload_queue.json"
_QUEUE_MAX = 500


class CloudCommunicator:
    def __init__(
        self,
        config: dict,
        get_conditions: Optional[Callable[[], dict]] = None,
        on_plan: Optional[Callable[[list], None]] = None,
        on_interrupt: Optional[Callable[[dict], None]] = None,
    ) -> None:
        cloud_cfg = config.get("cloud", {})
        self._url = str(cloud_cfg.get("url", "")).rstrip("/")
        self._heartbeat_s = float(cloud_cfg.get("heartbeat_interval", 60))
        self._plan_poll_s = float(cloud_cfg.get("plan_poll_interval", 300))
        self._upload_images = bool(cloud_cfg.get("upload_images", False))
        self._config = config
        self._get_conditions = get_conditions
        self._on_plan = on_plan
        self._on_interrupt = on_interrupt

        self._node_id = str(cloud_cfg.get("node_id", "") or "")
        # api_key is a secret: config carries a ${CLOUD_NODE_API_KEY} placeholder
        # that resolves from the environment.  An unset var expands to "" so the
        # node falls through to auto-registration as if no key were configured.
        self._api_key = str(expand_env(cloud_cfg.get("api_key", "")) or "")
        self._load_state()

        self._stop = threading.Event()
        self._queue_lock = threading.Lock()
        self._last_plan_id: Optional[str] = None
        self._threads: list[threading.Thread] = []

        # Status surface for the dashboard
        self.status: dict = {
            "registered": bool(self._node_id and self._api_key),
            "node_id": self._node_id,
            "last_heartbeat_ok": None,
            "last_plan_id": None,
            "plan_items": 0,
            "queued_uploads": len(self._load_queue()),
            "error": None,
        }

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._url:
            logger.error("cloud.url not configured — communicator not started")
            return
        for name, target in (("cloud-heartbeat", self._heartbeat_loop),
                             ("cloud-plan", self._plan_loop)):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        logger.info("Cloud communicator started → %s", self._url)

    def stop(self) -> None:
        self._stop.set()
        logger.info("Cloud communicator stopped")

    # ── Registration ───────────────────────────────────────────────────────────

    def _ensure_registered(self) -> bool:
        if self._node_id and self._api_key:
            return True

        obs = self._config.get("observatory", {})
        phot = self._config.get("photometry", {})
        cloud_cfg = self._config.get("cloud", {})
        payload = {
            "node_id":          phot.get("node_id", ""),
            "owner_name":       obs.get("observer", ""),
            "latitude":         obs.get("latitude") or 0.0,
            "longitude":        obs.get("longitude") or 0.0,
            "elevation":        obs.get("elevation", 0.0),
            "telescope_model":  obs.get("telescope", "ZWO Seestar S50"),
            "filters":          phot.get("filter_name", "CV"),
            "utc_offset_hours": _utc_offset_hours(),
        }
        # Include activation code on first boot if present in config
        activation_code = str(cloud_cfg.get("activation_code", "") or "").strip()
        if activation_code:
            payload["activation_code"] = activation_code
        try:
            resp = self._post("/api/v1/nodes/register", payload, auth=False)
        except Exception as exc:
            logger.warning("Cloud registration failed: %s", exc)
            self.status["error"] = f"registration failed: {exc}"
            return False
        self._node_id = resp["node_id"]
        self._api_key = resp["api_key"]
        self._save_state()
        self.status["registered"] = True
        self.status["node_id"] = self._node_id
        self.status["error"] = None
        logger.info("Registered with cloud as %s", self._node_id)
        return True

    def _load_state(self) -> None:
        """Credentials persisted from a previous auto-registration win over
        blank config values, never over explicit ones."""
        if self._node_id and self._api_key:
            return
        try:
            state = json.loads(_STATE_FILE.read_text())
            self._node_id = state.get("node_id", "")
            self._api_key = state.get("api_key", "")
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            _STATE_FILE.parent.mkdir(exist_ok=True)
            _STATE_FILE.write_text(json.dumps(
                {"node_id": self._node_id, "api_key": self._api_key}, indent=2))
        except OSError as exc:
            logger.warning("Could not persist cloud credentials: %s", exc)

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"X-Node-Id": self._node_id, "X-Api-Key": self._api_key}

    def _post(self, path: str, payload: dict, auth: bool = True) -> dict:
        import requests
        resp = requests.post(self._url + path, json=payload,
                             headers=self._headers() if auth else {}, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _get(self, path: str) -> dict:
        import requests
        resp = requests.get(self._url + path, headers=self._headers(), timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # ── Heartbeat loop ─────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            if self._ensure_registered():
                conditions = {}
                if self._get_conditions:
                    try:
                        conditions = self._get_conditions() or {}
                    except Exception as exc:
                        logger.debug("Conditions callback failed: %s", exc)
                conditions["utc_offset_hours"] = _utc_offset_hours()
                try:
                    self._post("/api/v1/nodes/heartbeat",
                               {"conditions": conditions})
                    self.status["last_heartbeat_ok"] = True
                    self.status["error"] = None
                except Exception as exc:
                    self.status["last_heartbeat_ok"] = False
                    self.status["error"] = str(exc)
                    logger.warning("Heartbeat failed: %s", exc)
                else:
                    self._flush_queue()
            self._stop.wait(self._heartbeat_s)

    # ── Plan / interrupt polling ───────────────────────────────────────────────

    def _plan_loop(self) -> None:
        while not self._stop.is_set():
            if self._node_id and self._api_key:
                try:
                    self._poll_plan()
                except Exception as exc:
                    logger.warning("Plan poll failed: %s", exc)
                try:
                    self._poll_interrupts()
                except Exception as exc:
                    logger.debug("Interrupt poll failed: %s", exc)
            self._stop.wait(self._plan_poll_s)

    def _poll_plan(self) -> None:
        data = self._get("/api/v1/plan")
        plan = data.get("plan")
        if not plan:
            return
        plan_id = plan.get("plan_id")
        self.status["last_plan_id"] = plan_id
        self.status["plan_items"] = len(plan.get("items", []))
        if plan_id == self._last_plan_id:
            return
        self._last_plan_id = plan_id
        items = plan.get("items", [])
        logger.info("New plan from cloud: %s (%d items, night %s)",
                    plan_id, len(items), plan.get("night", "?"))
        if self._on_plan and items:
            try:
                self._on_plan(items)
            except Exception as exc:
                logger.error("on_plan callback raised: %s", exc)

    def _poll_interrupts(self) -> None:
        data = self._get("/api/v1/interrupts")
        for item in data.get("interrupts", []):
            if item.get("acked"):
                continue
            logger.warning("Cloud interrupt: %s (%s)",
                           item.get("name"), item.get("reason", ""))
            if self._on_interrupt:
                try:
                    self._on_interrupt(item)
                except Exception as exc:
                    logger.error("on_interrupt callback raised: %s", exc)
            try:
                self._post(f"/api/v1/interrupts/{item['id']}/ack", {})
            except Exception as exc:
                logger.debug("Interrupt ack failed: %s", exc)

    # ── Measurement upload ─────────────────────────────────────────────────────

    def submit_measurement(self, measurement: dict,
                           conditions: Optional[dict] = None,
                           fits_path: Optional[str] = None) -> bool:
        """Upload one photometry result immediately. On failure, queue to disk
        for retry on the heartbeat cadence. Returns True when delivered now."""
        payload = {"measurement": measurement, "conditions": conditions or {}}
        if not (self._node_id and self._api_key):
            self._enqueue(payload)
            return False
        try:
            self._post("/api/v1/measurements", payload)
            logger.info("Measurement uploaded to cloud: %s mag=%.3f",
                        measurement.get("target_name", "?"),
                        measurement.get("magnitude", 0.0))
        except Exception as exc:
            logger.warning("Measurement upload failed — queued for retry: %s", exc)
            self._enqueue(payload)
            return False
        if fits_path and self._upload_images:
            self._upload_fits(fits_path)
        return True

    def upload_aavso_txt(self, txt_path: str) -> None:
        """Upload an AAVSO Extended File Format .txt to the cloud for later email submission."""
        try:
            import requests as _req
            from pathlib import Path
            with open(txt_path, "rb") as fh:
                resp = _req.post(
                    self._url + "/api/v1/aavso-files",
                    files={"file": (Path(txt_path).name, fh, "text/plain")},
                    headers=self._headers(), timeout=30)
            if resp.status_code == 200:
                logger.info("AAVSO file uploaded to cloud: %s", Path(txt_path).name)
            else:
                logger.warning("AAVSO file upload returned HTTP %d", resp.status_code)
        except Exception as exc:
            logger.warning("AAVSO file upload failed: %s", exc)

    def _upload_fits(self, fits_path: str) -> None:
        try:
            import requests
            with open(fits_path, "rb") as fh:
                resp = requests.post(
                    self._url + "/api/v1/images",
                    files={"file": (Path(fits_path).name, fh)},
                    headers=self._headers(), timeout=120)
            if resp.status_code == 200:
                logger.info("Raw FITS uploaded: %s", Path(fits_path).name)
            else:
                logger.warning("FITS upload returned HTTP %d", resp.status_code)
        except Exception as exc:
            logger.warning("FITS upload failed: %s", exc)

    # ── Disk-backed retry queue ────────────────────────────────────────────────

    def _load_queue(self) -> list:
        try:
            return json.loads(_QUEUE_FILE.read_text())
        except (OSError, ValueError):
            return []

    def _save_queue(self, queue: list) -> None:
        try:
            _QUEUE_FILE.parent.mkdir(exist_ok=True)
            _QUEUE_FILE.write_text(json.dumps(queue))
        except OSError as exc:
            logger.warning("Could not persist upload queue: %s", exc)

    def _enqueue(self, payload: dict) -> None:
        with self._queue_lock:
            queue = self._load_queue()
            queue.append(payload)
            if len(queue) > _QUEUE_MAX:
                queue = queue[-_QUEUE_MAX:]
            self._save_queue(queue)
            self.status["queued_uploads"] = len(queue)

    def _flush_queue(self) -> None:
        with self._queue_lock:
            queue = self._load_queue()
            if not queue:
                return
            remaining = []
            for payload in queue:
                try:
                    self._post("/api/v1/measurements", payload)
                except Exception:
                    remaining.append(payload)
            if len(remaining) != len(queue):
                logger.info("Flushed %d queued measurement(s) to cloud",
                            len(queue) - len(remaining))
            self._save_queue(remaining)
            self.status["queued_uploads"] = len(remaining)
