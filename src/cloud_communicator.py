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
      auto_run_plans: true   # hand new plans to the schedule runner automatically
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
import random
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from src.shared_models import expand_env

_PAIR_WORDS = [
    "NOVA","STAR","MOON","LENS","DOME","VEGA","LYRA","ORYX","CRAB","HALO",
    "FLUX","APEX","IRIS","MIRA","ARGO","ZETA","ARCH","BODE","COMA","DUSK",
]

def _make_pair_token() -> str:
    word = random.choice(_PAIR_WORDS)
    digits = random.randint(1000, 9999)
    return f"{word}-{digits}"

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
# Survey source lists are ~10-50 KB each, so this queue is capped by bytes
# rather than item count: ~20 MB rides out multi-day cloud outages without
# risking the disk the way an uncapped FITS backlog would.
_SURVEY_QUEUE_FILE = Path("data") / "survey_upload_queue.json"
_SURVEY_QUEUE_MAX_BYTES = 20 * 1024 * 1024


class CloudCommunicator:
    def __init__(
        self,
        config: dict,
        get_conditions: Optional[Callable[[], dict]] = None,
        on_plan: Optional[Callable[[list], None]] = None,
        on_interrupt: Optional[Callable[[dict], None]] = None,
        get_telescope_specs: Optional[Callable[[], dict]] = None,
        get_state: Optional[Callable[[], dict]] = None,
    ) -> None:
        cloud_cfg = config.get("cloud", {})
        self._url = str(cloud_cfg.get("url", "")).rstrip("/")
        self._heartbeat_s = float(cloud_cfg.get("heartbeat_interval", 60))
        self._plan_poll_s = float(cloud_cfg.get("plan_poll_interval", 300))
        self._upload_images = bool(cloud_cfg.get("upload_images", False))
        # THE ORGANISM: adaptive heartbeat + realtime push.
        # When the node is observing (dark/active) it heartbeats fast so its live
        # phase and any retasking arrive within seconds; idle/daytime it relaxes
        # to the slow cadence to save the fleet's request budget.
        self._heartbeat_fast_s = float(cloud_cfg.get("heartbeat_fast_interval", 5))
        # Realtime SSE gateway: separate host from the main API (own Railway
        # service). Falls back to url if unset; empty disables the SSE loop and
        # the node runs on polling alone (fully functional, just slower).
        self._realtime_url = str(cloud_cfg.get("realtime_url", "")).rstrip("/")
        self._config = config
        self._get_conditions = get_conditions
        self._get_telescope_specs = get_telescope_specs
        self._get_state = get_state
        self._on_plan = on_plan
        self._on_interrupt = on_interrupt

        self._node_id = str(cloud_cfg.get("node_id", "") or "")
        # api_key is a secret: config carries a ${CLOUD_NODE_API_KEY} placeholder
        # that resolves from the environment.  An unset var expands to "" so the
        # node falls through to auto-registration as if no key were configured.
        self._api_key = str(expand_env(cloud_cfg.get("api_key", "")) or "")
        self._pair_token: str = ""
        self._load_state()

        self._stop = threading.Event()
        self._queue_lock = threading.Lock()
        self._last_plan_id: Optional[str] = None
        self._threads: list[threading.Thread] = []
        self._register_failures = 0
        self._register_next_attempt = 0.0  # monotonic

        # Status surface for the dashboard
        self.status: dict = {
            "registered": bool(self._node_id and self._api_key),
            "node_id": self._node_id,
            "pair_token": self._pair_token,
            "last_heartbeat_ok": None,
            "last_plan_id": None,
            "plan_items": 0,
            "plan_pending_review": False,
            "queued_uploads": len(self._load_queue()),
            "queued_survey_uploads": len(self._load_survey_queue()),
            "error": None,
        }

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._url:
            logger.error("cloud.url not configured — communicator not started")
            return
        loops = [("cloud-heartbeat", self._heartbeat_loop),
                 ("cloud-plan", self._plan_loop)]
        if self._realtime_url or self._url:
            loops.append(("cloud-sse", self._sse_loop))
        if not (self._node_id and self._api_key):
            loops.append(("cloud-pair", self._pair_loop))
        for name, target in loops:
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        logger.info("Cloud communicator started → %s", self._url)
        if not (self._node_id and self._api_key) and self._pair_token:
            print(
                f"\n  ┌─────────────────────────────────────────────┐\n"
                f"  │  Not yet linked to The Telescope Net.       │\n"
                f"  │  Open the app, create an activation         │\n"
                f"  │  code, then paste it into the dashboard:    │\n"
                f"  │                                             │\n"
                f"  │      http://localhost:5173                  │\n"
                f"  └─────────────────────────────────────────────┘\n",
                flush=True,
            )

    def stop(self) -> None:
        self._stop.set()
        logger.info("Cloud communicator stopped")

    # ── Registration ───────────────────────────────────────────────────────────

    def _telescope_payload(self) -> dict:
        """
        Build the telescope-spec portion of the registration payload.

        Ground-truth precedence: live ALPACA device (``get_telescope_specs``
        callback) overrides the spec-catalog entry resolved from the configured
        model name.  Returns only the NodeInfo/registry telescope columns.
        """
        try:
            from src.telescope_specs import lookup, derive_params
        except Exception as exc:
            logger.debug("telescope_specs unavailable: %s", exc)
            return {}

        obs = self._config.get("observatory", {})
        model = obs.get("telescope") or obs.get("telescope_model") or ""
        derived: dict = {}
        spec = lookup(model)
        if spec is not None:
            derived = derive_params(spec)

        # Live ALPACA autodetect wins where it can report a value.
        if self._get_telescope_specs is not None:
            try:
                live = self._get_telescope_specs() or {}
                derived.update({k: v for k, v in live.items() if v not in (None, "", 0, 0.0)})
            except Exception as exc:
                logger.debug("Live telescope spec callback failed: %s", exc)

        if not derived:
            return {}

        # Select the columns the cloud nodes table actually stores; filter_set
        # must be a JSON string (registry stores it verbatim).
        keys = ("tier", "telescope_model", "aperture_mm", "focal_length_mm",
                "fov_deg", "pixel_scale_arcsec", "mount_type", "max_exposure_s",
                "camera_model", "cooled_camera", "mag_bright_limit", "mag_faint_limit")
        out = {k: derived[k] for k in keys if k in derived}
        if "filter_set" in derived:
            out["filter_set"] = json.dumps(derived["filter_set"])
        return out

    def _ensure_registered(self) -> bool:
        if self._node_id and self._api_key:
            return True

        # Back off between failed attempts (60 s → 2 min → … → 15 min) so an
        # invalid activation code doesn't hammer the API forever, while still
        # recovering quickly from a transient outage at first boot.
        now = time.monotonic()
        if now < self._register_next_attempt:
            return False

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
            "telescope_serial": obs.get("telescope_serial", ""),
            "telescope_name":   obs.get("telescope_name", ""),
            "filters":          phot.get("filter_name", "CV"),
            "utc_offset_hours": _utc_offset_hours(),
        }
        # Full telescope specs so the scheduler/photometry on the cloud knows
        # this node's optics & sensor.  Ground-truth order: live ALPACA device →
        # spec catalog (by model name) → leave to the cloud's column defaults.
        payload.update(self._telescope_payload())
        # Include activation code on first boot if present in config
        activation_code = str(cloud_cfg.get("activation_code", "") or "").strip()
        if activation_code:
            payload["activation_code"] = activation_code
        try:
            resp = self._post("/api/v1/nodes/register", payload, auth=False)
        except Exception as exc:
            logger.warning("Cloud registration failed: %s", exc)
            self.status["error"] = f"registration failed: {exc}"
            self._register_failures += 1
            backoff = min(60.0 * (2 ** min(self._register_failures - 1, 4)), 900.0)
            self._register_next_attempt = now + backoff
            if self._register_failures in (1, 5, 20):
                self._telemetry_event(
                    "registration_failed", "error",
                    {"error": str(exc)[:200],
                     "attempts": self._register_failures,
                     "retry_in_s": int(backoff)})
            return False
        self._register_failures = 0
        self._register_next_attempt = 0.0
        self._node_id = resp["node_id"]
        self._api_key = resp["api_key"]
        self._save_state()
        self.status["registered"] = True
        self.status["node_id"] = self._node_id
        self.status["error"] = None
        if activation_code:
            self._clear_activation_code()
        logger.info("Registered with cloud as %s", self._node_id)
        self._telemetry_event("registered", "info", {"node_id": self._node_id})
        return True

    def _clear_activation_code(self) -> None:
        """Remove the one-time activation code after successful registration."""
        try:
            from src.config_patch import apply_config_patch
            apply_config_patch({"cloud": {"activation_code": ""}})
        except Exception as exc:
            logger.warning("Could not clear activation code from config: %s", exc)
            return
        self._config.setdefault("cloud", {})["activation_code"] = ""
        logger.info("Activation code cleared from config after successful registration")

    def _load_state(self) -> None:
        """Credentials persisted from a previous auto-registration win over
        blank config values, never over explicit ones."""
        try:
            state = json.loads(_STATE_FILE.read_text())
            if not (self._node_id and self._api_key):
                self._node_id = state.get("node_id", "")
                self._api_key = state.get("api_key", "")
            self._pair_token = state.get("pair_token", "")
        except (OSError, ValueError):
            pass
        if not self._pair_token:
            self._pair_token = _make_pair_token()
            self._save_state()

    def _save_state(self) -> None:
        try:
            _STATE_FILE.parent.mkdir(exist_ok=True)
            _STATE_FILE.write_text(json.dumps(
                {"node_id": self._node_id, "api_key": self._api_key,
                 "pair_token": self._pair_token}, indent=2))
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

    def _current_state(self) -> dict:
        """Second-scale phase report for the live fleet map. Best-effort."""
        if not self._get_state:
            return {}
        try:
            return self._get_state() or {}
        except Exception as exc:
            logger.debug("State callback failed: %s", exc)
            return {}

    def _heartbeat_interval(self, state: dict) -> float:
        """Fast cadence while observing so retasking/live-state arrive within
        seconds; slow cadence when idle or in daylight to spare request budget."""
        phase = str(state.get("phase") or "").lower()
        if state.get("is_dark") or phase in (
                "slewing", "exposing", "stacking", "clouded"):
            return self._heartbeat_fast_s
        return self._heartbeat_s

    def _heartbeat_loop(self) -> None:
        was_ok: Optional[bool] = None
        while not self._stop.is_set():
            interval = self._heartbeat_s
            if self._ensure_registered():
                conditions = {}
                if self._get_conditions:
                    try:
                        conditions = self._get_conditions() or {}
                    except Exception as exc:
                        logger.debug("Conditions callback failed: %s", exc)
                conditions["utc_offset_hours"] = _utc_offset_hours()
                state = self._current_state()
                interval = self._heartbeat_interval(state)
                try:
                    self._post("/api/v1/nodes/heartbeat",
                               {"conditions": conditions, "state": state,
                                "heartbeat_s": interval})
                    self.status["last_heartbeat_ok"] = True
                    self.status["error"] = None
                except Exception as exc:
                    self.status["last_heartbeat_ok"] = False
                    self.status["error"] = str(exc)
                    logger.warning("Heartbeat failed: %s", exc)
                    if was_ok is not False:
                        self._telemetry_event(
                            "cloud_heartbeat_lost", "warning",
                            {"error": str(exc)[:200]})
                    was_ok = False
                else:
                    if was_ok is False:
                        self._telemetry_event("cloud_heartbeat_restored", "info", {})
                    was_ok = True
                    self._flush_queue()
                    self._flush_survey_queue()
            self._stop.wait(interval)

    # ── Realtime push (SSE) ─────────────────────────────────────────────────────

    def _sse_loop(self) -> None:
        """Hold a Server-Sent-Events connection to the realtime gateway.

        Events are pure signals: on 'plan'/'interrupt'/'retask' we immediately
        run the normal authenticated poll, so content is always fetched over the
        main API and the stream is never trusted for payloads. If the stream is
        unavailable the node loses nothing — the plan/interrupt polling loop is
        the correctness path; this just collapses its latency to ~1 s.
        """
        import requests
        base = self._realtime_url or self._url
        backoff = 3.0
        last_id = 0
        while not self._stop.is_set():
            if not (self._node_id and self._api_key):
                self._stop.wait(5)
                continue
            try:
                params = {"node_id": self._node_id, "api_key": self._api_key}
                headers = {"Accept": "text/event-stream"}
                if last_id:
                    headers["Last-Event-ID"] = str(last_id)
                with requests.get(base + "/api/v1/stream", params=params,
                                  headers=headers, stream=True,
                                  timeout=(10, 65)) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    backoff = 3.0  # connected — reset backoff
                    for raw in resp.iter_lines():
                        if self._stop.is_set():
                            return
                        if not raw:
                            continue
                        # text/event-stream carries no charset, so requests
                        # yields bytes; decode defensively either way.
                        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                        if line.startswith("id:"):
                            try:
                                last_id = int(line[3:].strip())
                            except ValueError:
                                pass
                        elif line.startswith("event:"):
                            self._on_sse_event(line[6:].strip())
            except Exception as exc:
                logger.debug("SSE connection dropped: %s", exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)

    def _on_sse_event(self, kind: str) -> None:
        """React to a push signal by fetching over the authenticated main API."""
        try:
            if kind in ("plan", "retask"):
                self._poll_plan()
                self._poll_interrupts()
            elif kind == "interrupt":
                self._poll_interrupts()
            elif kind == "config":
                self._poll_config_patches()
        except Exception as exc:
            logger.debug("SSE-triggered poll (%s) failed: %s", kind, exc)

    @staticmethod
    def _telemetry_event(name: str, severity: str, detail: dict) -> None:
        """Record a structured event without creating an import cycle at load."""
        try:
            from src import telemetry
            telemetry.event(name, severity=severity, detail=detail)
        except Exception:
            pass

    # ── Pairing loop (pre-registration only) ──────────────────────────────────

    def _pair_loop(self) -> None:
        """Poll the cloud for an activation code submitted by the app."""
        while not self._stop.is_set():
            if self._node_id and self._api_key:
                break  # already registered — exit this loop
            try:
                import requests
                resp = requests.get(
                    self._url + f"/api/v1/nodes/pair/{self._pair_token}",
                    timeout=15,
                )
                if resp.status_code == 200:
                    code = resp.json().get("code")
                    if code:
                        self._apply_paired_code(code)
                        break
            except Exception as exc:
                logger.debug("Pair poll failed: %s", exc)
            self._stop.wait(30)

    def _apply_paired_code(self, code: str) -> None:
        """Save the activation code to config and trigger immediate registration."""
        patch = {"cloud": {"activation_code": code, "enabled": True}}
        try:
            existing = self._config.get("cloud", {}).get("url", "")
            if not existing:
                patch["cloud"]["url"] = self._url
            from src.config_patch import apply_config_patch
            apply_config_patch(patch)
            logger.info("Activation code received via pairing and saved to config.yaml")
        except Exception as exc:
            logger.warning("Could not write activation code to config: %s", exc)
        self._config.setdefault("cloud", {})["activation_code"] = code
        self._config["cloud"]["enabled"] = True
        # A fresh code means any previous failure/backoff no longer applies.
        self._register_failures = 0
        self._register_next_attempt = 0.0
        self._ensure_registered()

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
                try:
                    self._poll_config_patches()
                except Exception as exc:
                    logger.debug("Config patch poll failed: %s", exc)
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

    def _poll_config_patches(self) -> None:
        data = self._get("/api/v1/nodes/config-patches")
        patches = data.get("patches") or []
        if not patches:
            return
        from src.config_patch import apply_config_patch
        import yaml

        for entry in patches:
            patch_id = entry.get("id")
            patch = entry.get("patch") or {}
            if not patch_id or not patch:
                continue
            try:
                apply_config_patch(patch)
                self._post(
                    f"/api/v1/nodes/config-patches/{patch_id}/ack",
                    {"ok": True},
                )
                try:
                    with open("config.yaml") as fh:
                        self._config = yaml.safe_load(fh) or {}
                except Exception:
                    pass
                logger.info("Applied cloud config patch %s", patch_id)
            except Exception as exc:
                logger.warning("Config patch %s failed: %s", patch_id, exc)
                try:
                    self._post(
                        f"/api/v1/nodes/config-patches/{patch_id}/ack",
                        {"ok": False, "error": str(exc)},
                    )
                except Exception:
                    pass

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

    def submit_survey(self, frame: dict, sources: list) -> bool:
        """Upload one frame's full-frame survey source list, gzip-compressed.

        On failure the payload queues to its own byte-capped disk queue and
        retries on the heartbeat cadence. Returns True when delivered now."""
        payload = {"frame": frame, "sources": sources}
        if not (self._node_id and self._api_key):
            self._enqueue_survey(payload)
            return False
        try:
            self._post_survey(payload)
            logger.info("Survey uploaded to cloud: %d sources from %s",
                        len(sources), frame.get("fits_file", "?"))
        except Exception as exc:
            logger.warning("Survey upload failed — queued for retry: %s", exc)
            self._enqueue_survey(payload)
            return False
        return True

    def _post_survey(self, payload: dict) -> None:
        import gzip
        import requests
        body = gzip.compress(json.dumps(payload).encode("utf-8"))
        headers = dict(self._headers())
        headers["Content-Type"] = "application/json"
        headers["Content-Encoding"] = "gzip"
        resp = requests.post(self._url + "/api/v1/survey", data=body,
                             headers=headers, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    def submit_characterization(self, report: dict) -> bool:
        """Report measured optics (pixel scale, FOV, FWHM, limiting mag) to
        the registry. Best-effort: the next report follows in ~25 solved
        frames, so failures are logged rather than queued."""
        if not (self._node_id and self._api_key):
            return False
        try:
            self._post("/api/v1/nodes/characterization", report)
            logger.info("Characterization reported: %s",
                        {k: v for k, v in report.items()
                         if k not in ("window_frames",)})
            return True
        except Exception as exc:
            logger.warning("Characterization report failed: %s", exc)
            return False

    def submit_incident(self, event: dict) -> None:
        """Forward one structured telemetry event to the cloud incident API.

        Best-effort: called from the telemetry forwarder thread; failures are
        swallowed (the event is still in the heartbeat summary and on disk).
        """
        if not (self._node_id and self._api_key):
            return
        self._post("/api/v1/incidents", {
            "incident_type": event.get("event", "unknown"),
            "severity": event.get("severity", "info"),
            "target_name": event.get("target", ""),
            "detail": event.get("detail") or {},
        })

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
            # Atomic replace: a crash mid-write must not corrupt the queue
            # file (a corrupt file silently discards every queued measurement).
            tmp = _QUEUE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(queue))
            tmp.replace(_QUEUE_FILE)
        except OSError as exc:
            logger.warning("Could not persist upload queue: %s", exc)

    def _enqueue(self, payload: dict) -> None:
        with self._queue_lock:
            queue = self._load_queue()
            queue.append(payload)
            dropped = len(queue) - _QUEUE_MAX
            if dropped > 0:
                queue = queue[-_QUEUE_MAX:]
                self._telemetry_event(
                    "upload_queue_overflow", "warning",
                    {"dropped": dropped, "queue_max": _QUEUE_MAX})
            self._save_queue(queue)
            self.status["queued_uploads"] = len(queue)

    def _flush_queue(self, max_items: int = 25, max_seconds: float = 60.0) -> None:
        """Retry queued uploads, time-boxed.

        Bounded per heartbeat cycle so draining a deep backlog after a long
        outage cannot monopolise the heartbeat thread (500 items × 30 s
        timeouts used to stall heartbeats for hours).  Stops at the first
        failure — if one upload fails the rest will too.
        """
        with self._queue_lock:
            queue = self._load_queue()
            if not queue:
                return
            deadline = time.monotonic() + max_seconds
            sent = 0
            failed = False
            remaining = list(queue)
            while remaining and sent < max_items and time.monotonic() < deadline:
                try:
                    self._post("/api/v1/measurements", remaining[0])
                except Exception:
                    failed = True
                    break
                remaining.pop(0)
                sent += 1
            if sent:
                logger.info("Flushed %d queued measurement(s) to cloud (%d left%s)",
                            sent, len(remaining),
                            ", will continue next heartbeat" if remaining and not failed else "")
            self._save_queue(remaining)
            self.status["queued_uploads"] = len(remaining)

    # ── Survey upload queue (byte-capped) ─────────────────────────────────────

    def _load_survey_queue(self) -> list:
        try:
            return json.loads(_SURVEY_QUEUE_FILE.read_text())
        except (OSError, ValueError):
            return []

    def _save_survey_queue(self, queue: list) -> None:
        try:
            _SURVEY_QUEUE_FILE.parent.mkdir(exist_ok=True)
            tmp = _SURVEY_QUEUE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(queue))
            tmp.replace(_SURVEY_QUEUE_FILE)
        except OSError as exc:
            logger.warning("Could not persist survey queue: %s", exc)

    def _enqueue_survey(self, payload: dict) -> None:
        with self._queue_lock:
            queue = self._load_survey_queue()
            queue.append(payload)
            # Enforce the byte cap by dropping oldest frames first: recent
            # frames are the ones deviation detection still cares about.
            dropped = 0
            while len(queue) > 1 and len(json.dumps(queue)) > _SURVEY_QUEUE_MAX_BYTES:
                queue.pop(0)
                dropped += 1
            if dropped:
                self._telemetry_event(
                    "survey_queue_overflow", "warning",
                    {"dropped_frames": dropped,
                     "max_bytes": _SURVEY_QUEUE_MAX_BYTES})
            self._save_survey_queue(queue)
            self.status["queued_survey_uploads"] = len(queue)

    def _flush_survey_queue(self, max_items: int = 10,
                            max_seconds: float = 30.0) -> None:
        """Retry queued survey uploads, time-boxed like _flush_queue."""
        with self._queue_lock:
            queue = self._load_survey_queue()
            if not queue:
                return
            deadline = time.monotonic() + max_seconds
            sent = 0
            remaining = list(queue)
            while remaining and sent < max_items and time.monotonic() < deadline:
                try:
                    self._post_survey(remaining[0])
                except Exception:
                    break
                remaining.pop(0)
                sent += 1
            if sent:
                logger.info("Flushed %d queued survey frame(s) to cloud (%d left)",
                            sent, len(remaining))
            self._save_survey_queue(remaining)
            self.status["queued_survey_uploads"] = len(remaining)
