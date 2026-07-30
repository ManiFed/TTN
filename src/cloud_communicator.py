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
    • registers automatically when no credentials exist; the node ID and
      pairing token live in data/cloud_state.json, while the API key lives in
      the operating system credential store
    • sends heartbeats with optional local conditions from a callback
    • polls for the current observation plan; when the plan_id changes,
      invokes on_plan(items, contingencies) with node-schedule-format items
      plus the plan's contingency ladder (alternates for local gap/failure fill)
    • polls interrupts; invokes on_interrupt(item) for unacked ones, then acks
    • submit_measurement() uploads each photometry result immediately;
      failures queue to disk and retry on the heartbeat cadence

Fully optional: when cloud.enabled is false (default) nothing starts and the
node behaves exactly as before.
"""

import json
import hashlib
import keyring
import logging
import math
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from src.shared_models import expand_env
from src.autonomy import AutonomyStore
from src.durable_outbox import DurableOutbox

_PAIR_WORDS = [
    "NOVA","STAR","MOON","LENS","DOME","VEGA","LYRA","ORYX","CRAB","HALO",
    "FLUX","APEX","IRIS","MIRA","ARGO","ZETA","ARCH","BODE","COMA","DUSK",
]

def _make_pair_token() -> str:
    word = random.choice(_PAIR_WORDS)
    digits = random.randint(1000, 9999)
    return f"{word}-{digits}"

logger = logging.getLogger("cloud_communicator")
AGENT_VERSION = "1.0.0"

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
_KEYRING_SERVICE = "the-telescope-node"
_KEYRING_ACCOUNT = "cloud-api-key"
_KEYRING_ACCOUNT_RECOVERY = "cloud-recovery-token"
_QUEUE_FILE = Path("data") / "cloud_upload_queue.json"
_QUEUE_MAX = 500
# Survey source lists are ~10-50 KB each, so this queue is capped by bytes
# rather than item count: ~20 MB rides out multi-day cloud outages without
# risking the disk the way an uncapped FITS backlog would.
_SURVEY_QUEUE_FILE = Path("data") / "survey_upload_queue.json"
_SURVEY_QUEUE_MAX_BYTES = 20 * 1024 * 1024
_OUTBOX_FILE = Path("data") / "node_outbox.db"


class CloudCommunicator:
    def __init__(
        self,
        config: dict,
        get_conditions: Optional[Callable[[], dict]] = None,
        on_plan: Optional[Callable[[list, dict], None]] = None,
        on_interrupt: Optional[Callable[[dict], None]] = None,
        on_task_cancel: Optional[Callable[[str], None]] = None,
        get_telescope_specs: Optional[Callable[[], dict]] = None,
        get_state: Optional[Callable[[], dict]] = None,
        on_location: Optional[Callable[[float, float], None]] = None,
    ) -> None:
        cloud_cfg = config.get("cloud", {})
        self._url = str(cloud_cfg.get("url", "")).rstrip("/")
        self._heartbeat_s = float(cloud_cfg.get("heartbeat_interval", 60))
        self._plan_poll_s = float(cloud_cfg.get("plan_poll_interval", 300))
        self._upload_images = bool(cloud_cfg.get("upload_images", False))
        # Live fleet: adaptive heartbeat + realtime push.
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
        self._on_task_cancel = on_task_cancel
        self._on_location = on_location
        self._last_observer: Optional[tuple] = None

        self._node_id = str(cloud_cfg.get("node_id", "") or "")
        # api_key is a secret: config carries a ${CLOUD_NODE_API_KEY} placeholder
        # that resolves from the environment.  An unset var expands to "" so the
        # node falls through to auto-registration as if no key were configured.
        self._api_key = str(expand_env(cloud_cfg.get("api_key", "")) or "")
        self._recovery_token: str = ""
        self._pair_token: str = ""
        self._load_state()

        self._stop = threading.Event()
        self._kick = threading.Event()
        self._queue_lock = threading.Lock()
        autonomy_cfg = config.get("autonomy") or {}
        self._outbox = DurableOutbox(
            _OUTBOX_FILE,
            max_bytes=int(autonomy_cfg.get("outbox_max_bytes", 2 * 1024**3)))
        # One-time upgrade only. The bounded JSON files below become diagnostic
        # mirrors after SQLite exists and must never be re-imported on restart.
        if self._outbox.count("measurement") == 0:
            self._outbox.migrate_json(_QUEUE_FILE, "measurement", priority=100)
        if self._outbox.count("survey") == 0:
            self._outbox.migrate_json(_SURVEY_QUEUE_FILE, "survey", priority=95)
        self._mirror_legacy_queues()
        self._autonomy = AutonomyStore()
        self._clock_samples: list[float] = []
        self._clock_reference: tuple[float, float] | None = None
        self._clock_qualified = self._autonomy.recent_clock_qualified()
        self._last_plan_id: Optional[str] = None
        self._seen_task_ids: set[str] = set()
        self._cancelled_task_ids: set[str] = set()
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
            "queued_uploads": self._outbox.count("measurement"),
            "queued_survey_uploads": self._outbox.count("survey"),
            "outbox_bytes": self._outbox.usage_bytes(),
            "outbox_capacity_bytes": self._outbox.max_bytes,
            "clock_skew_s": None,
            "clock_qualified": self._clock_qualified,
            "autonomy_bundle_id": "",
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
                "\n  ┌─────────────────────────────────────────────┐\n"
                "  │  Not yet linked to The Telescope Net.       │\n"
                "  │  Open The Telescope Net app, sign in,       │\n"
                f"  │  Connect telescope (pair: {self._pair_token:12s}) │\n"
                "  └─────────────────────────────────────────────┘\n",
                flush=True,
            )

    def stop(self) -> None:
        self._stop.set()
        logger.info("Cloud communicator stopped")

    def request_heartbeat(self) -> None:
        """Wake the heartbeat loop immediately instead of waiting out the interval.

        Call this whenever locally-known device state changes (connect,
        disconnect, reconnect) so the cloud's cached ``conditions`` — and
        anything a member's app derives from it, like "scope connected" —
        reflects reality within a second or two instead of lagging by up to
        a full heartbeat interval (60s idle, 600s at the reconnect backoff
        ceiling).
        """
        self._kick.set()

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
        self.install_credentials(resp["node_id"], resp["api_key"],
                                  recovery_token=resp.get("recovery_token", ""))
        logger.info("Registered with cloud as %s (unlinked — use app Connect telescope)",
                    self._node_id)
        self._telemetry_event("registered", "info", {"node_id": self._node_id})
        return True

    def credentials(self) -> tuple:
        """Current (node_id, api_key). Either may be "" when unregistered.

        For the local member app, which needs them to claim this node rather
        than register a duplicate. The api_key is never put in `status`,
        which the dashboard serves widely.
        """
        return self._node_id, self._api_key

    def install_credentials(self, node_id: str, api_key: str,
                             recovery_token: Optional[str] = None) -> None:
        """Install cloud credentials from the signed-in member app (or register).

        recovery_token is omitted (not cleared) when the caller doesn't have
        one to offer -- e.g. the app pushing credentials for an existing node
        doesn't know its recovery_token, and dropping a good one here would
        break self-recovery for no reason."""
        self._node_id = str(node_id or "").strip()
        self._api_key = str(api_key or "").strip()
        if recovery_token:
            self._recovery_token = str(recovery_token).strip()
        self._save_state()
        self.status["registered"] = bool(self._node_id and self._api_key)
        self.status["node_id"] = self._node_id
        self.status["error"] = None
        self._register_failures = 0
        self._register_next_attempt = 0.0

    def _load_state(self) -> None:
        """Credentials persisted from a previous auto-registration win over
        blank config values, never over explicit ones."""
        migrated_legacy_secret = False
        try:
            state = json.loads(_STATE_FILE.read_text())
            if not (self._node_id and self._api_key):
                self._node_id = state.get("node_id", "")
                self._api_key = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT) or ""
                # Migrate credentials written by earlier releases, then remove
                # the clear-text key from the state file on the next save.
                if not self._api_key and state.get("api_key"):
                    keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, state["api_key"])
                    self._api_key = state["api_key"]
                    migrated_legacy_secret = True
                self._recovery_token = keyring.get_password(
                    _KEYRING_SERVICE, _KEYRING_ACCOUNT_RECOVERY) or ""
            self._pair_token = state.get("pair_token", "")
        except (OSError, ValueError, keyring.errors.KeyringError) as exc:
            logger.warning("Could not load cloud credentials from the system keyring: %s", exc)
        if migrated_legacy_secret:
            self._save_state()
        if not self._pair_token:
            self._pair_token = _make_pair_token()
            self._save_state()

    def _save_state(self) -> None:
        # API keys must never be written to cloud_state.json.  A node without
        # a usable system credential store has to pair/register again after a
        # restart rather than leave a reusable credential on disk.
        if self._api_key:
            try:
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, self._api_key)
            except keyring.errors.KeyringError as exc:
                logger.warning(
                    "Could not persist cloud API key to the system keyring; "
                    "it will not survive a restart: %s", exc)
        if self._recovery_token:
            try:
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT_RECOVERY,
                                      self._recovery_token)
            except keyring.errors.KeyringError as exc:
                logger.warning(
                    "Could not persist cloud recovery token to the system keyring; "
                    "a future revoked api_key won't self-heal without it: %s", exc)
        payload = {"node_id": self._node_id, "pair_token": self._pair_token}
        try:
            _STATE_FILE.parent.mkdir(exist_ok=True)
            _STATE_FILE.write_text(json.dumps(payload, indent=2))
        except OSError as exc:
            logger.warning("Could not persist cloud_state.json: %s", exc)

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"X-Node-Id": self._node_id, "X-Api-Key": self._api_key}

    def _maybe_report_location(self, observer: Optional[dict]) -> None:
        """Invoke on_location the first time we hear the effective observer
        location, and again whenever it changes -- e.g. a portable node
        moved to a new site and started a session there. The node agent has
        no other way to learn this; it only knows its own config-file
        coordinates otherwise."""
        if not observer or not self._on_location:
            return
        try:
            lat = float(observer["latitude"])
            lon = float(observer["longitude"])
        except (TypeError, ValueError, KeyError):
            return
        current = (round(lat, 4), round(lon, 4))
        if current == self._last_observer:
            return
        self._last_observer = current
        try:
            self._on_location(lat, lon)
        except Exception as exc:
            logger.debug("on_location callback failed: %s", exc)

    def _post(self, path: str, payload: dict, auth: bool = True) -> dict:
        import requests
        resp = requests.post(self._url + path, json=payload,
                             headers=self._headers() if auth else {}, timeout=30)
        if auth and resp.status_code == 401:
            self._handle_unauthorized(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _get(self, path: str) -> dict:
        import requests
        resp = requests.get(self._url + path, headers=self._headers(), timeout=30)
        if resp.status_code == 401:
            self._handle_unauthorized(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _handle_unauthorized(self, resp) -> None:
        """The cloud rejects our node_id/api_key outright (as opposed to a
        transient/5xx/429 failure) — the stale credential will never start
        working again on retry. Try to recover silently first: if we hold a
        recovery_token for this exact node_id, trade it for a fresh api_key
        and keep going under the same identity (same history, no re-linking).
        Only if that's unavailable or also rejected do we fall back to
        wiping credentials and registering as a brand new, unlinked node."""
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if body.get("error") != "invalid node credentials":
            return
        if self._recovery_token and self._rekey():
            return
        self._clear_credentials("invalid node credentials")

    def _rekey(self) -> bool:
        """POST the recovery_token to trade it for a fresh api_key without
        losing node_id (see cloud/registry.py rekey_node). Deliberately not
        routed through _post()/_get() -- those call _handle_unauthorized on
        401, which would recurse back into this method."""
        import requests
        try:
            resp = requests.post(
                self._url + "/api/v1/nodes/rekey",
                json={"node_id": self._node_id, "recovery_token": self._recovery_token},
                timeout=30)
        except requests.RequestException as exc:
            logger.warning("Recovery-token rekey request failed: %s", exc)
            return False
        if resp.status_code != 200:
            logger.warning("Recovery-token rekey rejected (HTTP %d) — "
                            "falling back to fresh registration", resp.status_code)
            return False
        body = resp.json()
        self.install_credentials(self._node_id, body["api_key"],
                                  recovery_token=body.get("recovery_token", ""))
        logger.info("Recovered node %s via recovery_token — same identity, no re-linking needed",
                    self._node_id)
        self._telemetry_event("credentials_rekeyed", "info", {"node_id": self._node_id})
        return True

    def _clear_credentials(self, reason: str) -> None:
        logger.warning("Cloud rejected stored node credentials (%s) — "
                       "clearing and re-registering", reason)
        self._node_id = ""
        self._api_key = ""
        self._recovery_token = ""
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except keyring.errors.KeyringError:
            pass
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT_RECOVERY)
        except keyring.errors.KeyringError:
            pass
        self._save_state()
        self.status["registered"] = False
        self.status["node_id"] = ""
        self.status["error"] = f"credentials rejected by cloud ({reason}) — re-registering"
        self._telemetry_event("credentials_rejected", "error", {"reason": reason})

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
                    sent_at = time.time()
                    resp = self._post("/api/v1/nodes/heartbeat",
                                      {"conditions": conditions, "state": state,
                                       "heartbeat_s": interval,
                                       "clock_skew_s": self.status.get("clock_skew_s"),
                                       "clock_qualified": self._clock_qualified})
                    self._record_clock_sample(resp.get("server_time"), sent_at,
                                              time.time())
                    self.status["last_heartbeat_ok"] = True
                    self.status["error"] = None
                    self._maybe_report_location(resp.get("observer"))
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
                    self._flush_telemetry_queue()
                    self._flush_execution_outcomes()
            self._wait_or_kick(interval)

    def _wait_or_kick(self, interval: float) -> None:
        """Sleep for *interval* seconds, waking early on stop() or request_heartbeat()."""
        step = 1.0
        elapsed = 0.0
        while elapsed < interval and not self._stop.is_set() and not self._kick.is_set():
            self._stop.wait(min(step, interval - elapsed))
            elapsed += step
        self._kick.clear()

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
                self._poll_tasks()
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
        """Poll the cloud for credentials pushed by the signed-in member app."""
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
                    data = resp.json()
                    node_id = data.get("node_id")
                    api_key = data.get("api_key")
                    if node_id and api_key:
                        self.install_credentials(node_id, api_key)
                        logger.info("Credentials received via pairing → %s", node_id)
                        break
            except Exception as exc:
                logger.debug("Pair poll failed: %s", exc)
            self._stop.wait(30)

    # ── Plan / interrupt polling ───────────────────────────────────────────────

    def _plan_loop(self) -> None:
        while not self._stop.is_set():
            if self._node_id and self._api_key:
                try:
                    self._poll_autonomy_bundle()
                except Exception as exc:
                    logger.debug("Autonomy bundle poll failed: %s", exc)
                try:
                    self._poll_plan()
                except Exception as exc:
                    logger.warning("Plan poll failed: %s", exc)
                try:
                    self._poll_interrupts()
                except Exception as exc:
                    logger.debug("Interrupt poll failed: %s", exc)
                try:
                    self._poll_tasks()
                except Exception as exc:
                    logger.debug("Observation task poll failed: %s", exc)
                try:
                    self._poll_config_patches()
                except Exception as exc:
                    logger.debug("Config patch poll failed: %s", exc)
            self._stop.wait(self._plan_poll_s)

    def _record_clock_sample(self, server_time: str, sent_at: float,
                             received_at: float) -> None:
        if not server_time:
            return
        try:
            server = datetime.fromisoformat(str(server_time).replace("Z", "+00:00"))
            if server.tzinfo is None:
                server = server.replace(tzinfo=timezone.utc)
            midpoint = (sent_at + received_at) / 2.0
            skew = midpoint - server.timestamp()
        except (TypeError, ValueError):
            return
        monotonic_now = time.monotonic()
        if self._clock_reference is not None:
            wall_delta = received_at - self._clock_reference[0]
            mono_delta = monotonic_now - self._clock_reference[1]
            if abs(wall_delta - mono_delta) > 5.0:
                self._clock_samples.clear()
                self._clock_qualified = False
                self._telemetry_event("clock_jump", "error",
                                      {"wall_delta_s": wall_delta, "monotonic_delta_s": mono_delta})
        self._clock_reference = (received_at, monotonic_now)
        self._clock_samples = (self._clock_samples + [skew])[-4:]
        self._clock_qualified = (len(self._clock_samples) >= 2
                                 and all(abs(v) <= 30.0 for v in self._clock_samples[-2:])
                                 and abs(self._clock_samples[-1] - self._clock_samples[-2]) <= 30.0)
        self.status["clock_skew_s"] = round(skew, 3)
        self.status["clock_qualified"] = self._clock_qualified
        self._autonomy.set_clock_qualified(self._clock_qualified, skew)
        if abs(skew) > 60.0:
            self._telemetry_event("clock_skew", "error", {"skew_s": round(skew, 3)})

    def _poll_autonomy_bundle(self) -> None:
        if not (self._config.get("autonomy") or {}).get("enabled", False):
            return
        data = self._get("/api/v1/autonomy/bundle")
        bundle = data.get("bundle")
        if not bundle:
            return
        keys = dict((self._config.get("autonomy") or {}).get("public_keys") or {})
        commissioning_complete = False
        try:
            commissioning_complete = json.loads(
                (Path("data") / "commissioning.json").read_text()).get("status") == "complete"
        except Exception:
            pass
        acfg = self._config.get("autonomy") or {}
        obs = self._config.get("observatory") or {}
        try:
            float(obs["latitude"]); float(obs["longitude"])
            location_valid = math.isfinite(float(obs["latitude"])) and math.isfinite(float(obs["longitude"]))
        except (KeyError, TypeError, ValueError):
            location_valid = False
        accepted = self._autonomy.verify_and_store(
            bundle, self._node_id, keys, agent_version=AGENT_VERSION,
            commissioning_complete=commissioning_complete,
            location_valid=location_valid,
            max_storage_bytes=int(acfg.get("max_bundle_storage_bytes", 20 * 1024**3)))
        self.status["autonomy_bundle_id"] = accepted["bundle_id"]

    def active_autonomy_bundle(self) -> Optional[dict]:
        return self._autonomy.active(self._node_id,
                                     clock_qualified=self._clock_qualified)

    def outbox_at_capacity(self) -> bool:
        """Backpressure gate used before starting another offline item."""
        used = self._outbox.usage_bytes()
        self.status["outbox_bytes"] = used
        return used >= self._outbox.max_bytes

    def invalidate_clock_trust(self, reason: str = "clock jump") -> None:
        self._clock_qualified = False
        self._clock_samples.clear()
        self.status["clock_qualified"] = False
        self._autonomy.set_clock_qualified(False, float(self.status.get("clock_skew_s") or 0))
        self._telemetry_event("clock_jump", "error", {"reason": reason})

    def autonomy_remaining_items(self) -> list:
        bundle = self.active_autonomy_bundle()
        return self._autonomy.remaining_items(bundle) if bundle else []

    def autonomy_contingency_items(self) -> list:
        bundle = self.active_autonomy_bundle()
        if not bundle:
            return []
        items = self._autonomy.contingency_items(bundle)
        for item in items:
            item["bundle_id"] = bundle["bundle_id"]
        return items

    def _flush_execution_outcomes(self) -> None:
        outcomes = self._autonomy.pending_outcomes()
        if not outcomes:
            return
        response = self._post("/api/v1/nodes/execution-outcomes",
                              {"outcomes": outcomes})
        if response.get("ok"):
            self._autonomy.mark_uploaded([o["attempt_id"] for o in outcomes])

    def record_execution_outcome(self, bundle_id: str, item_id: str,
                                 state: str, **kwargs) -> str:
        return self._autonomy.record(bundle_id, item_id, state, **kwargs)

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
        contingencies = plan.get("contingencies") or {}
        bundle = self.active_autonomy_bundle()
        if bundle and bundle.get("plan_id") == plan_id:
            items = self._autonomy.remaining_items(bundle)
            for item in items:
                item["bundle_id"] = bundle["bundle_id"]
        logger.info("New plan from cloud: %s (%d items, %d alternates, night %s)",
                    plan_id, len(items), len(contingencies.get("alternates", [])),
                    plan.get("night", "?"))
        if self._on_plan and items:
            try:
                self._on_plan(items, contingencies)
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

    def _poll_tasks(self) -> None:
        data = self._get("/api/v1/tasks")
        for task_id in data.get("cancelled_task_ids", []):
            task_id = str(task_id)
            if task_id not in self._cancelled_task_ids and self._on_task_cancel:
                self._on_task_cancel(task_id)
            self._cancelled_task_ids.add(task_id)
        for item in data.get("tasks", []):
            task_id = str(item.get("task_id") or "")
            if not task_id or task_id in self._seen_task_ids:
                continue
            if self._on_interrupt:
                self._on_interrupt(item)
            self._seen_task_ids.add(task_id)

    def task_cancelled(self, task_id: str) -> bool:
        return str(task_id) in self._cancelled_task_ids

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
            queued = dict(payload)
            if fits_path:
                queued["_raw_fits_path"] = str(fits_path)
            self._enqueue(queued)
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
        public = dict(payload)
        public.pop("_raw_fits_path", None)
        if isinstance(public.get("frame"), dict):
            public["frame"] = dict(public["frame"])
            public["frame"].pop("_raw_fits_path", None)
        body = gzip.compress(json.dumps(public).encode("utf-8"))
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

        Failures enter the low-priority SQLite outbox. Science records can
        evict this telemetry if the configured byte budget becomes tight.
        """
        payload = {
            "incident_type": event.get("event", "unknown"),
            "severity": event.get("severity", "info"),
            "target_name": event.get("target", ""),
            "detail": event.get("detail") or {},
            "idempotency_key": hashlib.sha256(json.dumps(
                event, sort_keys=True, separators=(",", ":"), default=str
            ).encode()).hexdigest(),
        }
        if not (self._node_id and self._api_key):
            self._outbox.enqueue("telemetry", payload, priority=20)
            return
        try:
            self._post("/api/v1/incidents", payload)
        except Exception:
            self._outbox.enqueue("telemetry", payload, priority=20)

    def _flush_telemetry_queue(self, max_items: int = 50,
                               max_seconds: float = 10.0) -> None:
        deadline = time.monotonic() + max_seconds
        for row in self._outbox.peek("telemetry", max_items):
            if time.monotonic() >= deadline:
                break
            try:
                self._post("/api/v1/incidents", row["payload"])
            except Exception:
                self._outbox.failed(row["id"])
                break
            self._outbox.ack(row["id"])

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
        rows = [r["payload"] for r in self._outbox.peek("measurement", 100000)]
        return rows[-_QUEUE_MAX:]  # compatibility view; SQLite retains the full backlog

    def _save_queue(self, queue: list) -> None:
        self._outbox.clear("measurement")
        for payload in queue:
            self._outbox.enqueue("measurement", payload, priority=100)
        self._mirror_legacy_queues()

    def _enqueue(self, payload: dict) -> None:
        with self._queue_lock:
            measurement = payload.get("measurement") or {}
            identity = (measurement.get("idempotency_key") or "measurement:" +
                        hashlib.sha256(json.dumps(
                            payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
            self._outbox.enqueue("measurement", payload, priority=100,
                                 idempotency_key=str(identity))
            self.status["queued_uploads"] = self._outbox.count("measurement")
            self.status["outbox_bytes"] = self._outbox.usage_bytes()
            if self.status["queued_uploads"] > _QUEUE_MAX:
                self._telemetry_event("upload_queue_overflow", "warning",
                                      {"sqlite_retained": self.status["queued_uploads"],
                                       "legacy_view": _QUEUE_MAX})
            self._mirror_legacy_queues()

    def _flush_queue(self, max_items: int = 25, max_seconds: float = 60.0) -> None:
        """Retry queued uploads, time-boxed.

        Bounded per heartbeat cycle so draining a deep backlog after a long
        outage cannot monopolise the heartbeat thread (500 items × 30 s
        timeouts used to stall heartbeats for hours).  Stops at the first
        failure — if one upload fails the rest will too.
        """
        with self._queue_lock:
            rows = self._outbox.peek("measurement", max_items)
            if not rows:
                return
            deadline = time.monotonic() + max_seconds
            sent = 0
            for row in rows:
                if time.monotonic() >= deadline:
                    break
                try:
                    public = dict(row["payload"])
                    public.pop("_raw_fits_path", None)
                    self._post("/api/v1/measurements", public)
                except Exception:
                    self._outbox.failed(row["id"])
                    break
                self._outbox.ack(row["id"])
                sent += 1
            if sent:
                logger.info("Flushed %d queued measurement(s) to cloud", sent)
            self.status["queued_uploads"] = self._outbox.count("measurement")
            self._mirror_legacy_queues()

    # ── Survey upload queue (byte-capped) ─────────────────────────────────────

    def _load_survey_queue(self) -> list:
        return [r["payload"] for r in self._outbox.peek("survey", 100000)]

    def _save_survey_queue(self, queue: list) -> None:
        self._outbox.clear("survey")
        for payload in queue:
            self._outbox.enqueue("survey", payload, priority=95)
        self._mirror_legacy_queues()

    def _enqueue_survey(self, payload: dict) -> None:
        with self._queue_lock:
            frame = payload.get("frame") or {}
            identity = f"{self._node_id}:{frame.get('fits_file','')}:{frame.get('bjd','')}"
            self._outbox.enqueue("survey", payload, priority=95,
                                 idempotency_key=identity)
            self.status["queued_survey_uploads"] = self._outbox.count("survey")
            self.status["outbox_bytes"] = self._outbox.usage_bytes()
            self._mirror_legacy_queues()

    def _flush_survey_queue(self, max_items: int = 10,
                            max_seconds: float = 30.0) -> None:
        """Retry queued survey uploads, time-boxed like _flush_queue."""
        with self._queue_lock:
            rows = self._outbox.peek("survey", max_items)
            if not rows:
                return
            deadline = time.monotonic() + max_seconds
            sent = 0
            for row in rows:
                if time.monotonic() >= deadline:
                    break
                try:
                    self._post_survey(row["payload"])
                except Exception:
                    self._outbox.failed(row["id"])
                    break
                self._outbox.ack(row["id"])
                sent += 1
            if sent:
                logger.info("Flushed %d queued survey frame(s) to cloud", sent)
            self.status["queued_survey_uploads"] = self._outbox.count("survey")
            self._mirror_legacy_queues()

    def _mirror_legacy_queues(self) -> None:
        """Keep bounded JSON diagnostics while SQLite remains authoritative.

        This also makes upgrades reversible: older agents can inspect the tail
        without imposing their former record ceiling on retained science data.
        """
        try:
            measurements = [r["payload"] for r in self._outbox.peek("measurement", 100000)]
            if len(measurements) > _QUEUE_MAX:
                measurements = measurements[-_QUEUE_MAX:]
            surveys = [r["payload"] for r in self._outbox.peek("survey", 100000)]
            kept, used = [], 2
            for payload in reversed(surveys):
                size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) + 1
                if used + size > _SURVEY_QUEUE_MAX_BYTES:
                    continue
                kept.append(payload)
                used += size
            kept.reverse()
            if len(kept) < len(surveys):
                self._telemetry_event("survey_queue_overflow", "warning",
                                      {"sqlite_retained": len(surveys),
                                       "legacy_view": len(kept)})
            for path, value in ((_QUEUE_FILE, measurements), (_SURVEY_QUEUE_FILE, kept)):
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(value, separators=(",", ":")))
                tmp.replace(path)
        except Exception as exc:
            logger.debug("Could not refresh legacy queue mirror: %s", exc)
