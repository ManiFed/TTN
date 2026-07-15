#!/usr/bin/env python3
"""
Cloud API — Flask app serving nodes today and the member dashboard / mobile
app tomorrow.

Node endpoints (X-Node-Id + X-Api-Key headers, except register):
    POST /api/v1/nodes/register          → {node_id, api_key}
    POST /api/v1/nodes/heartbeat         body: {"conditions": {...}} (optional)
    GET  /api/v1/nodes/me                → own registry entry
    GET  /api/v1/plan                    → current ObservationPlan JSON
    POST /api/v1/measurements            body: {"measurement": {...}, "conditions": {...}}
    POST /api/v1/images                  multipart: file=<fits>
    POST /api/v1/aavso-files             multipart: file=<txt>  (upload AAVSO .txt)
    GET  /api/v1/aavso-files             → list of uploaded .txt files
    GET  /api/v1/aavso-files/download/<path>  → download one file
    GET  /api/v1/interrupts              → unexpired interrupts for this node

Public/query endpoints (for dashboard & app):
    GET  /api/v1/targets                 → active targets with best scores
    GET  /api/v1/lightcurves/<name>      → aggregated light curve
    GET  /api/v1/network/status          → node + data summary
    GET  /api/v1/weather?lat=&lon=       → astronomy weather forecast (7timer ASTRO)
    GET  /api/v1/light-pollution?lat=&lon= → sky brightness (mpsas, bortle, source)

Admin endpoints (X-Admin-Key header):
    POST /api/v1/interrupts              → broadcast a high-priority target
    POST /api/v1/admin/ingest            → run alert ingestion now
    POST /api/v1/admin/replan            → rescore + regenerate all plans
    GET  /api/v1/admin/tuning            → active scoring weights + tuning history
    POST /api/v1/admin/tuning/rollback   → restore the previous scoring weights
"""

import json
import logging
import os
import re
import secrets
import string
import threading as _threading
import time as _time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from defusedxml import ElementTree as ET
from flask import Flask, jsonify, redirect as _redirect, request, send_from_directory
from werkzeug.utils import safe_join

from cloud import alerts, auth, autonomy, calibration, data_pipeline, db, gcn_events, help_chat, incidents, live, nights, registry, scheduler, scoring, survey, tuning
from src.shared_models import science_program_for_type
from cloud.conditions import fetch_astronomy_weather, fetch_light_pollution_detail

logger = logging.getLogger("cloud.server")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024

_config: dict = {}   # set by create_app()

_WEBSITE_DIR = os.path.join(os.path.dirname(__file__), "..", "website")
_DASHBOARD_DIR = os.path.join(_WEBSITE_DIR, "dashboard")


def create_app(config: dict) -> Flask:
    global _config
    _config = config
    return app


@app.route("/")
def serve_index():
    return send_from_directory(_WEBSITE_DIR, "tour.html")


@app.route("/dashboard")
@app.route("/dashboard/")
def serve_dashboard():
    return send_from_directory(_DASHBOARD_DIR, "index.html")


@app.route("/dashboard/<path:filename>")
def serve_dashboard_asset(filename):
    full = safe_join(_DASHBOARD_DIR, filename)
    if full and os.path.isfile(full):
        return send_from_directory(_DASHBOARD_DIR, filename)
    return send_from_directory(_DASHBOARD_DIR, "index.html")


@app.route("/<path:filename>")
def serve_website(filename):
    # Never let the website catch-all swallow API requests — return 404 so Flask
    # still has a chance to route them, and so debugging is obvious.
    if filename.startswith("api/"):
        return jsonify({"error": "not found"}), 404
    full = safe_join(_WEBSITE_DIR, filename)
    if full and os.path.isfile(full):
        return send_from_directory(_WEBSITE_DIR, filename)
    return send_from_directory(_WEBSITE_DIR, "tour.html")


# ── Software downloads ─────────────────────────────────────────────────────────
# GitHub Releases are the canonical source. The endpoint redirects so the URL
# on the website stays stable even as release tags change.

_GITHUB_LATEST_RELEASE = "https://github.com/ManiFed/TTN/releases/latest/download"
_GITHUB_RELEASE_PAGE = "https://github.com/ManiFed/TTN/releases/latest"

# Stable, version-agnostic asset names: the release workflow uploads assets
# under these exact names on every tag, so this link never goes stale again
# (previously hardcoded a version tag + filename that rotted the moment a new
# release shipped without this file being updated to match).
_DOWNLOAD_URLS = {
    "macos":   f"{_GITHUB_LATEST_RELEASE}/TelescopeNetNode-macOS.pkg",
    "windows": os.environ.get("TTN_WINDOWS_DOWNLOAD_URL", _GITHUB_RELEASE_PAGE),
    "linux":   os.environ.get("TTN_RASPIOS_DOWNLOAD_URL", _GITHUB_RELEASE_PAGE),
    "raspberry-pi-os": os.environ.get("TTN_RASPIOS_DOWNLOAD_URL", _GITHUB_RELEASE_PAGE),
    "raspbian": os.environ.get("TTN_RASPIOS_DOWNLOAD_URL", _GITHUB_RELEASE_PAGE),
}

@app.route("/download/node-agent")
@app.route("/download/node-agent/<platform>")
def download_node_agent(platform: str = "macos"):
    url = _DOWNLOAD_URLS.get(platform.strip().lower())
    if url is None:
        return jsonify({"error": f"'{platform}' installer not yet available"}), 404
    return _redirect(url, code=302)


# Cached lookup of the newest published release tag, so the app/node agent can
# nag members to update instead of silently drifting out of sync the way
# v1.0.4 did. Cached for a few minutes — GitHub's API is rate-limited and this
# value changes at most a few times a month.
_latest_version_cache: dict = {"version": None, "checked_at": 0.0}
_LATEST_VERSION_TTL = 300.0

def _fetch_latest_version() -> str | None:
    now = _time.time()
    if _latest_version_cache["version"] and now - _latest_version_cache["checked_at"] < _LATEST_VERSION_TTL:
        return _latest_version_cache["version"]
    try:
        import requests
        resp = requests.get(
            "https://api.github.com/repos/ManiFed/TTN/releases/latest", timeout=5)
        resp.raise_for_status()
        tag = resp.json().get("tag_name", "")
        version = tag[1:] if tag.startswith("v") else tag
    except Exception as exc:
        logger.warning("Could not fetch latest release version: %s", exc)
        return _latest_version_cache["version"]
    _latest_version_cache["version"] = version or None
    _latest_version_cache["checked_at"] = now
    return _latest_version_cache["version"]


@app.route("/api/v1/versions", methods=["GET"])
def api_versions():
    """Newest published node/app version, for update-nag banners.

    Node agent + Flutter app are built and released together from one tag, so
    a single version string covers every platform.
    """
    version = _fetch_latest_version()
    return jsonify({
        "latest": version,
        "download_page": _GITHUB_RELEASE_PAGE,
    })


@app.after_request
def _cors(resp):
    """Allow the marketing site / dashboard (served from another origin in dev)
    to read the public JSON endpoints from the browser."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Node-Id, X-Api-Key, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    return resp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Auth decorators ────────────────────────────────────────────────────────────

def require_node(fn):
    """Authenticate via X-Node-Id / X-Api-Key; passes the node row as `node`."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        node = registry.authenticate(
            request.headers.get("X-Node-Id", ""),
            request.headers.get("X-Api-Key", ""),
        )
        if node is None:
            return jsonify({"error": "invalid node credentials"}), 401
        return fn(node, *args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        admin_key = _config.get("server", {}).get("admin_key", "")
        if not admin_key or request.headers.get("X-Admin-Key", "") != admin_key:
            return jsonify({"error": "invalid admin key"}), 401
        return fn(*args, **kwargs)
    return wrapper


# ── Node management ────────────────────────────────────────────────────────────

def _geocode_location(name: str) -> tuple[float | None, float | None]:
    """Resolve a place name to (lat, lon) via Nominatim. Returns (None, None) on failure."""
    try:
        import requests as _req
        resp = _req.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": name, "format": "json", "limit": 1},
            headers={"User-Agent": "TheTelescopeCloud/1.0"},
            timeout=8,
        )
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as exc:
        logger.warning("Geocode failed for '%s': %s", name, exc)
    return None, None


def _generate_activation_code(year: int | None = None) -> str:
    """Generate a unique BS-YYYY-XXXXXXXX activation code."""
    y = year or datetime.now(timezone.utc).year
    chars = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(8))
    return f"BS-{y}-{suffix}"


def _validate_and_consume_code(code: str, node_id: str) -> str | None:
    """
    Validate an activation code and mark it consumed.
    Returns the associated user_id (may be None for generic codes), or raises
    ValueError if the code is invalid, expired, or already used.
    """
    row = db.query_one("SELECT * FROM activation_codes WHERE code = %s", (code,))
    if row is None:
        raise ValueError(f"activation code not found: {code}")
    if row["used_at"]:
        raise ValueError("activation code already used")
    if row["expires_at"] and row["expires_at"] < _now():
        raise ValueError("activation code expired")

    db.execute(
        "UPDATE activation_codes SET used_at = %s, node_id = %s WHERE code = %s",
        (_now(), node_id, code),
    )
    return row["user_id"]  # may be None


_pair_store: dict = {}
_pair_lock = _threading.Lock()

def _pair_gc() -> None:
    now = _time.time()
    with _pair_lock:
        stale = [k for k, v in _pair_store.items() if v["expires_at"] < now]
        for k in stale:
            del _pair_store[k]

@app.route("/api/v1/nodes/pair", methods=["POST"])
@auth.require_member
def api_pair_submit(user):
    """App pushes node credentials to a pairing token the local agent is polling.

    Body: {pairing_token, node_id, api_key}
    (Activation codes are retired — credentials come from /me/nodes/attach.)
    """
    body = request.get_json(force=True, silent=True) or {}
    token = str(body.get("pairing_token") or "").strip().upper()
    node_id = str(body.get("node_id") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    if not token or not node_id or not api_key:
        return jsonify({"error": "pairing_token, node_id, and api_key required"}), 400
    # Confirm the member owns this node (or just attached it).
    if not db.query_one(
        "SELECT 1 FROM node_members WHERE node_id = %s AND user_id = %s",
        (node_id, user["user_id"]),
    ):
        return jsonify({"error": "not your node"}), 403
    node = registry.authenticate(node_id, api_key)
    if node is None:
        return jsonify({"error": "invalid node credentials"}), 401
    _pair_gc()
    with _pair_lock:
        _pair_store[token] = {
            "node_id": node_id,
            "api_key": api_key,
            "expires_at": _time.time() + 1800,
        }
    logger.info("Pairing credentials stored for token %s by member %s → %s",
                token, user["user_id"], node_id)
    return jsonify({"ok": True})

@app.route("/api/v1/nodes/pair/<token>", methods=["GET"])
def api_pair_claim(token):
    """Node polls this to receive credentials. Consumes the entry."""
    token = token.strip().upper()
    _pair_gc()
    with _pair_lock:
        entry = _pair_store.pop(token, None)
    if not entry:
        return jsonify({"node_id": None, "api_key": None})
    logger.info("Pairing claimed for token %s → %s", token, entry.get("node_id"))
    return jsonify({"node_id": entry["node_id"], "api_key": entry["api_key"]})


@app.route("/api/v1/nodes/register", methods=["POST"])
def api_register():
    """Anonymous/self registration for a node agent.

    Does NOT link to a member account. Members link via POST /me/nodes/attach
    (signed-in app) or POST /me/nodes/<id> (claim with credentials).
    Activation codes are no longer accepted.
    """
    info = request.get_json(force=True, silent=True) or {}
    if info.pop("activation_code", None):
        return jsonify({
            "error": "activation codes are retired — sign in to the app and "
                     "use Connect telescope to link this computer",
        }), 410

    try:
        creds = registry.register_node(
            info, _config.get("light_pollution", {}).get("api_key", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(creds)


@app.route("/api/v1/nodes/characterization", methods=["POST"])
@require_node
def api_characterization(node):
    """Self-measured optics from the node's own plate solves. A dedicated
    endpoint: re-POSTing register would overwrite owner/location wholesale,
    and heartbeat conditions only land in a JSON blob nothing reads."""
    body = request.get_json(force=True, silent=True) or {}
    result = registry.update_characterization(node["node_id"], body)
    return (jsonify(result), 200) if result.get("ok") else (jsonify(result), 400)


@app.route("/api/v1/nodes/heartbeat", methods=["POST"])
@require_node
def api_heartbeat(node):
    body = request.get_json(force=True, silent=True) or {}
    registry.heartbeat(node["node_id"], body.get("conditions"))
    if body.get("clock_skew_s") is not None:
        try:
            skew = float(body["clock_skew_s"])
            db.execute("UPDATE nodes SET clock_skew_s=%s,clock_qualified_at=%s WHERE node_id=%s",
                       (skew, _now() if body.get("clock_qualified") else "", node["node_id"]))
        except (TypeError, ValueError):
            pass
    # Live fleet state: fold the optional second-scale phase report into the
    # live fleet map. Old node agents omit "state" and simply don't appear live.
    state = body.get("state")
    if isinstance(state, dict):
        try:
            live.record_state(
                node["node_id"], state,
                heartbeat_s=float(body.get("heartbeat_s") or 60.0))
        except Exception as exc:
            logger.debug("live.record_state failed for %s: %s", node["node_id"], exc)
    # Current effective observer location (session location for a portable
    # node with an active session, else the node's fixed coordinates). The
    # node agent only knows its own config-file location otherwise, so a
    # portable node moved to a new site never told its own safety/horizon
    # logic -- this lets it notice the change and re-run a horizon scan.
    if bool(node.get("portable")) and node.get("session_lat") not in (None, 0, 0.0):
        observer = {"latitude": node["session_lat"], "longitude": node["session_lon"]}
    else:
        observer = {"latitude": node["latitude"], "longitude": node["longitude"]}
    return jsonify({"ok": True, "server_time": _now(), "observer": observer})


@app.route("/api/v1/incidents", methods=["POST"])
@require_node
def api_node_incident(node):
    """Structured incident reported by a node agent (telemetry forwarder).

    Feeds the same reliability_incidents table the cloud writes to, so slew
    failures, emergency parks, disk exhaustion etc. are visible to a remote
    operator the moment they happen instead of the morning after.
    """
    body = request.get_json(force=True, silent=True) or {}
    incident_type = str(body.get("incident_type") or "").strip()
    if not incident_type:
        return jsonify({"error": "incident_type required"}), 400
    severity = str(body.get("severity") or "info").lower()
    if severity not in ("info", "warning", "error", "critical"):
        severity = "info"
    detail = body.get("detail")
    if not isinstance(detail, dict):
        detail = {"raw": str(detail)[:500]} if detail else {}
    # Bound stored detail so a chatty node can't bloat the incidents table.
    detail_json = json.dumps(detail)
    if len(detail_json) > 4000:
        detail = {"truncated": detail_json[:4000]}
    incidents.log(
        node["node_id"],
        incident_type,
        severity=severity,
        target_name=str(body.get("target_name") or ""),
        detail=detail,
        idempotency_key=str(body.get("idempotency_key") or ""),
    )
    return jsonify({"ok": True})


@app.route("/api/v1/nodes/me", methods=["GET"])
@require_node
def api_node_me(node):
    return jsonify(registry.public_view(node))


# ── Plans ──────────────────────────────────────────────────────────────────────

@app.route("/api/v1/plan", methods=["GET"])
@require_node
def api_plan(node):
    plan = scheduler.current_plan(node["node_id"])
    if plan is None:
        # Generate on demand the first time a node asks
        generated = scheduler.generate_plan(node, _config)
        plan = generated.to_dict() if generated else None
    if plan is None:
        return jsonify({"plan": None, "message": "no observable night window"}), 200
    return jsonify({"plan": plan})


@app.route("/api/v1/autonomy/bundle", methods=["GET"])
@require_node
def api_autonomy_bundle(node):
    try:
        if scheduler.current_plan(node["node_id"]) is None:
            scheduler.generate_plan(node, _config)
        bundle = autonomy.build_for_node(node["node_id"], _config)
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc), "bundle": None}), 503
    return jsonify({"bundle": bundle,
                    "public_key": autonomy.public_key_b64(_config) if bundle else ""})


@app.route("/api/v1/nodes/execution-outcomes", methods=["POST"])
@require_node
def api_execution_outcomes(node):
    body = request.get_json(force=True, silent=True) or {}
    outcomes = body.get("outcomes") or []
    if not isinstance(outcomes, list):
        return jsonify({"error": "outcomes must be a list"}), 400
    return jsonify(autonomy.store_outcomes(node["node_id"], outcomes))


@app.route("/api/v1/tasks", methods=["GET"])
@require_node
def api_observation_tasks(node):
    """Live-connectivity-only event tasks; signed bundles never contain these."""
    rows = db.query(
        "SELECT * FROM observation_tasks WHERE node_id=%s AND state IN ('pending','received') "
        "AND latest_utc>%s ORDER BY priority DESC,earliest_utc,task_id",
        (node["node_id"], _now()))
    out = []
    for row in rows:
        exposure = db.loads(row.get("exposure"), {})
        out.append({
            "task_id": row["task_id"], "item_id": row["task_id"],
            "target": f"{row['event_id']} / {row['tile_id']}",
            "ra": round(float(row["ra_deg"]) / 15.0, 7), "dec": row["dec_deg"],
            "ra_deg": row["ra_deg"], "dec_deg": row["dec_deg"],
            "starts_at_utc": row["earliest_utc"], "latest_start_utc": row["latest_utc"],
            "task_type": "event_tile", "campaign_id": row["event_id"],
            "priority": row["priority"],
            "cancellation_generation": row["cancellation_generation"],
            "observation_mode": "single_epoch", "processing_mode": "event_tile",
            **exposure,
        })
        db.execute("UPDATE observation_tasks SET state='received',updated_at=%s "
                   "WHERE task_id=%s AND state='pending'", (_now(), row["task_id"]))
    cancelled = [r["task_id"] for r in db.query(
        "SELECT task_id FROM observation_tasks WHERE node_id=%s AND state='cancelled'",
        (node["node_id"],))]
    return jsonify({"tasks": out, "cancelled_task_ids": cancelled})


# ── Network events and coverage ──────────────────────────────────────────────

@app.route("/api/v1/events/<event_id>", methods=["GET"])
def api_network_event(event_id):
    event = db.query_one("SELECT * FROM network_events WHERE event_id=%s", (event_id,))
    if not event:
        return jsonify({"error": "unknown event"}), 404
    event = dict(event)
    event["policy"] = db.loads(event.get("policy"), {})
    revisions = db.query(
        "SELECT * FROM event_revisions WHERE event_id=%s ORDER BY revision", (event_id,))
    for rev in revisions:
        for field in ("significance", "localization", "distance"):
            rev[field] = db.loads(rev.get(field), {})
        rev.pop("raw_notice", None)
    return jsonify({"event": event, "revisions": revisions})


@app.route("/api/v1/events/<event_id>/coverage", methods=["GET"])
def api_network_event_coverage(event_id):
    event = db.query_one("SELECT * FROM network_events WHERE event_id=%s", (event_id,))
    if not event:
        return jsonify({"error": "unknown event"}), 404
    try:
        revision = int(request.args.get("revision", event["active_revision"]))
    except (TypeError, ValueError):
        return jsonify({"error": "revision must be an integer"}), 400
    if not db.query_one("SELECT 1 FROM event_revisions WHERE event_id=%s AND revision=%s",
                        (event_id, revision)):
        return jsonify({"error": "unknown event revision"}), 404
    tiles = db.query("SELECT * FROM event_tiles WHERE event_id=%s AND event_revision=%s",
                     (event_id, revision))
    tasks = db.query("SELECT * FROM observation_tasks WHERE event_id=%s AND event_revision=%s",
                     (event_id, revision))
    for task in tasks:
        task["result"] = db.loads(task.get("result"), {})
    # Revisit tasks confirm candidates but do not cover new probability mass.
    # Counting only first-pass tiles prevents second epochs from inflating
    # scheduled/observed sky coverage above the localization's total mass.
    by_tile = {r["tile_id"]: (float(r.get("probability_mass") or 0)
                              if int(r.get("pass_number") or 1) == 1 else 0.0)
               for r in tiles}
    scheduled_ids = {r["tile_id"] for r in tasks if r["state"] != "shadow"}
    observed_ids = {r["tile_id"] for r in tasks if r["state"] == "completed"}
    footprints = {(round(float(r["ra_deg"]), 5), round(float(r["dec_deg"]), 5))
                  for r in tasks}
    duplicates = max(0, len(tasks) - len(footprints))
    depth_weight = sum(by_tile.get(r["tile_id"], 0)
                       * float((r.get("result") or {}).get("limiting_magnitude") or 0)
                       for r in tasks if (r.get("result") or {}).get("limiting_magnitude"))
    depth_mass = sum(by_tile.get(r["tile_id"], 0) for r in tasks
                     if (r.get("result") or {}).get("limiting_magnitude"))
    candidate_count = (db.query_one(
        "SELECT COUNT(DISTINCT d.id) AS n FROM discovery_candidates d "
        "JOIN survey_measurements s ON s.source_key=d.source_key WHERE s.event_id=%s",
        (event_id,)) or {}).get("n", 0)
    return jsonify({
        "event_id": event_id, "revision": revision,
        "status": event["status"],
        "probability_scheduled": sum(by_tile.get(v, 0) for v in scheduled_ids),
        "probability_observed": sum(by_tile.get(v, 0) for v in observed_ids),
        "probability_weighted_limiting_magnitude": depth_weight / depth_mass if depth_mass else None,
        "galaxy_weighted_coverage": sum(by_tile.get(v, 0) for v in observed_ids),
        "tiles": len(tiles), "tasks": len(tasks),
        "duplicate_coverage_fraction": duplicates / max(1, len(tasks)),
        "node_failures": sum(1 for r in tasks if r["state"] == "failed"),
        "candidate_count": int(candidate_count or 0),
    })


@app.route("/api/v1/admin/events", methods=["GET"])
@require_admin
def api_admin_events():
    rows = db.query("SELECT * FROM network_events ORDER BY received_time DESC LIMIT 500")
    for row in rows:
        row["policy"] = db.loads(row.get("policy"), {})
    return jsonify({"events": rows})


@app.route("/api/v1/admin/events/<event_id>/cancel", methods=["POST"])
@require_admin
def api_admin_event_cancel(event_id):
    if not gcn_events.cancel(event_id):
        return jsonify({"error": "unknown event"}), 404
    return jsonify({"ok": True, "event_id": event_id})


# ── Measurements & images ──────────────────────────────────────────────────────

@app.route("/api/v1/measurements", methods=["POST"])
@require_node
def api_measurements(node):
    body = request.get_json(force=True, silent=True) or {}
    measurement = body.get("measurement") or body   # accept bare measurement dicts
    result = data_pipeline.ingest_measurement(
        node["node_id"], measurement, body.get("conditions"))
    return (jsonify(result), 200) if result.get("ok") else (jsonify(result), 400)


@app.route("/api/v1/survey", methods=["POST"])
@require_node
def api_survey(node):
    """Full-frame survey source lists. Nodes gzip these (~10-50 KB/frame);
    Flask does not transparently decompress, so handle it here."""
    raw = request.get_data()
    if request.headers.get("Content-Encoding", "").lower() == "gzip":
        import gzip as _gzip
        try:
            raw = _gzip.decompress(raw)
        except OSError:
            return jsonify({"error": "bad gzip body"}), 400
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return jsonify({"error": "bad json body"}), 400
    result = survey.ingest_batch(node["node_id"], body, _config,
                                 node_tier=int(node.get("tier") or 1))
    return (jsonify(result), 200) if result.get("ok") else (jsonify(result), 400)


@app.route("/api/v1/images", methods=["POST"])
@require_node
def api_images(node):
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "no file in upload"}), 400
    path = data_pipeline.store_raw_image(
        node["node_id"], f.filename or "image.fits", f.read(), _config)
    if path is None:
        return jsonify({"error": "image rejected or storage failed"}), 400
    return jsonify({"ok": True, "stored": path})


# ── AAVSO Extended File Format uploads ────────────────────────────────────────
# Nodes POST their per-observation .txt files here; anyone can list/download
# them so the operator can email them to observations@aavso.org.

def _aavso_file_dir() -> "Path":
    d = Path(_config.get("storage", {}).get("aavso_file_dir", "cloud_data/aavso_files"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.route("/api/v1/aavso-files", methods=["POST"])
@require_node
def api_aavso_files_upload(node):
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "no file"}), 400
    raw = f.read()
    if len(raw) > 512 * 1024:
        return jsonify({"error": "file too large"}), 400
    # Sanitise filename and place under cloud_data/aavso_files/<date>/
    from pathlib import Path
    import re as _re
    safe_name = _re.sub(r"[^A-Za-z0-9_.\-]", "_", f.filename or "obs.txt")
    if not safe_name.endswith(".txt"):
        safe_name += ".txt"
    date_dir = _aavso_file_dir() / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    dest = date_dir / safe_name
    # Append a counter suffix if a file with that name already exists
    counter = 1
    while dest.exists():
        stem = Path(safe_name).stem
        dest = date_dir / f"{stem}_{counter}.txt"
        counter += 1
    dest.write_bytes(raw)
    rel = str(dest.relative_to(_aavso_file_dir()))
    logger.info("AAVSO file stored: %s (node=%s)", rel, node["node_id"])
    return jsonify({"ok": True, "path": rel})


@app.route("/api/v1/aavso-files", methods=["GET"])
def api_aavso_files_list():
    root = _aavso_file_dir()
    files = []
    for txt in sorted(root.rglob("*.txt"), reverse=True):
        rel = str(txt.relative_to(root))
        files.append({
            "path":     rel,
            "size":     txt.stat().st_size,
            "modified": datetime.fromtimestamp(txt.stat().st_mtime, tz=timezone.utc).isoformat(),
            "download": f"/api/v1/aavso-files/download/{rel}",
        })
    return jsonify({"files": files, "count": len(files)})


@app.route("/api/v1/aavso-files/download/<path:rel>", methods=["GET"])
def api_aavso_files_download(rel):
    from pathlib import Path
    # Guard against path traversal
    root = _aavso_file_dir()
    abs_path = safe_join(str(root.resolve()), rel)
    if abs_path is None:
        return jsonify({"error": "invalid path"}), 400
    if not Path(abs_path).is_file():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(str(root.resolve()), rel, as_attachment=True,
                               download_name=Path(rel).name)


def _mpc_report_dir() -> Path:
    d = Path((_config.get("mpc", {}) or {}).get("report_dir", "cloud_data/mpc_reports"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.route("/api/v1/mpc-files", methods=["GET"])
def api_mpc_files_list():
    """Generated MPC ADES astrometry reports, for an operator to download
    and manually submit via MPC's web upload."""
    root = _mpc_report_dir()
    files = []
    for psv in sorted(root.rglob("*.psv"), reverse=True):
        rel = str(psv.relative_to(root))
        files.append({
            "path":     rel,
            "size":     psv.stat().st_size,
            "modified": datetime.fromtimestamp(psv.stat().st_mtime, tz=timezone.utc).isoformat(),
            "download": f"/api/v1/mpc-files/download/{rel}",
        })
    return jsonify({"files": files, "count": len(files)})


@app.route("/api/v1/mpc-files/download/<path:rel>", methods=["GET"])
def api_mpc_files_download(rel):
    from pathlib import Path
    root = _mpc_report_dir()
    abs_path = safe_join(str(root.resolve()), rel)
    if abs_path is None:
        return jsonify({"error": "invalid path"}), 400
    if not Path(abs_path).is_file():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(str(root.resolve()), rel, as_attachment=True,
                               download_name=Path(rel).name)


# ── Interrupts ─────────────────────────────────────────────────────────────────

@app.route("/api/v1/interrupts", methods=["GET"])
@require_node
def api_interrupts_get(node):
    rows = db.query(
        "SELECT * FROM interrupts WHERE expires_at > %s", (_now(),))
    out = []
    for r in rows:
        node_ids = db.loads(r["node_ids"], None)
        if node_ids and node["node_id"] not in node_ids:
            continue
        acked = db.loads(r["acked_by"], [])
        out.append({
            "id": r["id"], "name": r["name"],
            "ra_deg": r["ra_deg"], "dec_deg": r["dec_deg"],
            "ra": round(r["ra_deg"] / 15.0, 4), "dec": round(r["dec_deg"], 4),
            "mag": r["mag"], "reason": r["reason"],
            "created_at": r["created_at"], "expires_at": r["expires_at"],
            "acked": node["node_id"] in acked,
        })
    return jsonify({"interrupts": out})


@app.route("/api/v1/interrupts/<int:interrupt_id>/ack", methods=["POST"])
@require_node
def api_interrupt_ack(node, interrupt_id: int):
    row = db.query_one("SELECT acked_by FROM interrupts WHERE id = %s", (interrupt_id,))
    if row is None:
        return jsonify({"error": "unknown interrupt"}), 404
    acked = db.loads(row["acked_by"], [])
    if node["node_id"] not in acked:
        acked.append(node["node_id"])
        db.execute("UPDATE interrupts SET acked_by = %s WHERE id = %s",
                   (json.dumps(acked), interrupt_id))
    return jsonify({"ok": True})


@app.route("/api/v1/interrupts", methods=["POST"])
@require_admin
def api_interrupts_post():
    body = request.get_json(force=True, silent=True) or {}
    try:
        name = str(body["name"])
        ra_deg = float(body["ra_deg"])
        dec_deg = float(body["dec_deg"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "name, ra_deg, dec_deg required"}), 400
    hours = float(body.get("expires_hours", 12.0))
    raw_node_ids = body.get("node_ids")
    if isinstance(raw_node_ids, str):
        node_ids = [raw_node_ids]
    elif isinstance(raw_node_ids, list):
        node_ids = [str(n) for n in raw_node_ids if str(n).strip()]
    else:
        node_ids = None
    if not node_ids and body.get("escalate", True):
        node_ids = _eligible_interrupt_nodes(
            {"name": name, "ra_deg": ra_deg, "dec_deg": dec_deg, "mag": body.get("mag")},
            min_score=float(body.get("min_score", 0.35)),
        )
    iid = db.execute(
        """INSERT INTO interrupts
               (target_id, name, ra_deg, dec_deg, mag, reason, node_ids,
                created_at, expires_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (body.get("target_id"), name, ra_deg, dec_deg, body.get("mag"),
         str(body.get("reason", "")),
         json.dumps(node_ids) if node_ids else None,
         _now(),
         (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()),
        returning_id=True,
    )
    logger.info("Interrupt #%d created: %s (%.4f, %.4f)", iid, name, ra_deg, dec_deg)
    if body.get("replan", False) and node_ids:
        for nid in node_ids:
            node = db.query_one("SELECT * FROM nodes WHERE node_id = %s", (nid,))
            if node:
                scheduler.generate_plan(node, _config)
    return jsonify({"ok": True, "id": iid, "node_ids": node_ids or []})


def _eligible_interrupt_nodes(target: dict, min_score: float = 0.35) -> list[str]:
    """Choose active nodes that can plausibly see a transient interrupt soon."""
    from cloud.conditions import night_window
    out: list[tuple[float, str]] = []
    pseudo = {
        "target_id": "interrupt",
        "name": target["name"],
        "ra_deg": target["ra_deg"],
        "dec_deg": target["dec_deg"],
        "mag": target.get("mag"),
        "target_type": "transient",
        "priority": 1.0,
        "time_critical": 1,
        "cadence_hours": 1,
        "discovered_at": _now(),
    }
    for node in registry.list_nodes():
        if node.get("status") in ("disabled", "vacation"):
            continue
        try:
            night = night_window(node["latitude"], node["longitude"])
            weather = scoring.weather_factor(node, night)
            comp = scoring.score_target_for_node(pseudo, node, night, weather, _config)
            if comp["total"] >= min_score:
                out.append((comp["total"], node["node_id"]))
        except Exception as exc:
            logger.warning("Interrupt eligibility failed for %s: %s", node.get("node_id"), exc)
    out.sort(reverse=True)
    return [nid for _, nid in out[:50]]


# ── Query endpoints (dashboard / app) ──────────────────────────────────────────

@app.route("/api/v1/targets", methods=["GET"])
def api_targets():
    rows = db.query(
        """SELECT t.*, MAX(s.total) AS best_score,
                  COUNT(DISTINCT m.id) AS n_measurements
           FROM targets t
           LEFT JOIN scores s ON s.target_id = t.target_id
           LEFT JOIN measurements m ON m.target_name = t.name
           WHERE t.active = 1
           GROUP BY t.target_id ORDER BY best_score DESC LIMIT 200""")
    for r in rows:
        r["sources"] = db.loads(r["sources"], [])
        r["science_program"] = science_program_for_type(r.get("target_type") or "")
        best = db.query_one(
            """SELECT node_id, components FROM scores
               WHERE target_id = %s ORDER BY total DESC LIMIT 1""",
            (r["target_id"],),
        )
        comp = db.loads((best or {}).get("components"), {})
        r["best_node_id"] = (best or {}).get("node_id", "")
        r["score_explanation"] = comp.get("explanation", {})
    return jsonify({"targets": rows})


@app.route("/api/v1/lightcurves/<path:target_name>", methods=["GET"])
def api_lightcurve(target_name: str):
    days = float(request.args.get("days", 365))
    points = data_pipeline.light_curve(target_name, days)
    return jsonify({"target": target_name, "n": len(points), "points": points})


@app.route("/api/v1/lightcurves/<path:target_name>/consensus", methods=["GET"])
def api_consensus_lightcurve(target_name: str):
    """
    Inverse-variance-weighted consensus light curve for epochs where 2+ nodes
    observed the same target in the same co-temporal window (~43 min).
    Each point carries n_nodes and node_ids so callers can filter by coverage.
    """
    days = float(request.args.get("days", 365))
    points = data_pipeline.consensus_light_curve(target_name, days)
    return jsonify({"target": target_name, "n": len(points), "points": points})


def _sesame_lookup(name: str) -> dict:
    import requests

    resp = requests.get(
        f"https://cds.unistra.fr/cgi-bin/nph-sesame/-oxp/SNV?{quote(name)}",
        timeout=12,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Sesame HTTP {resp.status_code}")

    root = ET.fromstring(resp.text)
    resolver = root.find(".//Resolver")
    if resolver is None:
        return {}

    def text(path: str) -> str:
        el = resolver.find(path)
        return (el.text or "").strip() if el is not None else ""

    mags = {}
    for mag in resolver.findall(".//mag"):
        band = (mag.attrib.get("band") or "").strip()
        value = (mag.text or "").strip()
        if band and value:
            mags[band] = value

    aliases = [
        (el.text or "").strip()
        for el in resolver.findall(".//alias")
        if (el.text or "").strip()
    ]
    return {
        "source": "CDS Sesame",
        "canonical_name": text("oname") or name,
        "ra_deg": float(text("jradeg")) if text("jradeg") else None,
        "dec_deg": float(text("jdedeg")) if text("jdedeg") else None,
        "object_type": text("otype"),
        "spectral_type": text("spType"),
        "magnitudes": mags,
        "aliases": aliases[:12],
    }


def _nasa_exoplanet_lookup(name: str) -> dict:
    import requests

    if not re.search(r"\s+[bcdefghij]$", name.strip(), re.I):
        return {}

    safe_name = name.replace("'", "''")
    query = (
        "select pl_name,hostname,sy_dist,sy_vmag,pl_orbper,pl_trandur,"
        "pl_trandep,pl_rade,pl_bmasse,disc_year,discoverymethod "
        f"from pscomppars where pl_name='{safe_name}'"
    )
    resp = requests.get(
        "https://exoplanetarchive.ipac.caltech.edu/TAP/sync",
        params={"query": query, "format": "json"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"NASA Exoplanet Archive HTTP {resp.status_code}")
    rows = resp.json()
    if not rows:
        return {}
    row = rows[0]
    return {
        "source": "NASA Exoplanet Archive",
        "planet_name": row.get("pl_name"),
        "host_name": row.get("hostname"),
        "distance_pc": row.get("sy_dist"),
        "v_mag": row.get("sy_vmag"),
        "period_days": row.get("pl_orbper"),
        "transit_duration_hours": row.get("pl_trandur"),
        "transit_depth_ppm": row.get("pl_trandep"),
        "radius_earth": row.get("pl_rade"),
        "mass_earth": row.get("pl_bmasse"),
        "discovery_year": row.get("disc_year"),
        "discovery_method": row.get("discoverymethod"),
    }


@app.route("/api/v1/objects/<path:object_name>", methods=["GET"])
def api_object_details(object_name: str):
    """Public catalogue details for the selected target."""
    details = {
        "name": object_name,
        "public_sources": [],
        "errors": [],
    }
    for fetcher in (_sesame_lookup, _nasa_exoplanet_lookup):
        try:
            data = fetcher(object_name)
        except Exception as exc:
            logger.info("Object lookup failed for %s: %s", object_name, exc)
            # Provider exceptions can include URLs, credentials, or library
            # internals.  Keep diagnostics in the server log only.
            details["errors"].append("A catalogue source was unavailable")
            continue
        if not data:
            continue
        source = data.pop("source", "")
        if source:
            details["public_sources"].append(source)
        details.update({k: v for k, v in data.items() if v not in (None, "", {})})
    return jsonify(details)


@app.route("/api/v1/network/status", methods=["GET"])
def api_network_status():
    nodes = [registry.public_view(n) for n in registry.list_nodes()]
    meas = db.query_one("SELECT COUNT(*) AS n FROM measurements") or {"n": 0}
    meas_24h = db.query_one(
        "SELECT COUNT(*) AS n FROM measurements WHERE received_at > %s",
        ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
    ) or {"n": 0}
    targets = db.query_one("SELECT COUNT(*) AS n FROM targets WHERE active = 1") or {"n": 0}
    submitted = db.query_one(
        "SELECT COUNT(*) AS n FROM measurements WHERE aavso_submitted = 1") or {"n": 0}
    return jsonify({
        "nodes_total":          len(nodes),
        "nodes_online":         sum(1 for n in nodes if n["online"]),
        "active_targets":       targets["n"],
        "measurements_total":   meas["n"],
        "measurements_24h":     meas_24h["n"],
        "aavso_submitted":      submitted["n"],
        "nodes":                nodes,
        "server_time":          _now(),
    })


@app.route("/api/v1/network/fleet", methods=["GET"])
def api_network_fleet():
    """Live fleet state: second-scale phase of every node in the fleet.

    Public read (like network/status) so the member 'live fleet' view and the
    node dashboard can render who is dark, clouded, exposing, or offline right
    now. Nodes appear here only after they start sending heartbeat 'state'.
    """
    fleet = live.fleet_state()
    # Fold in static location so the client can place each node on the globe
    # without a second call; live rows carry only volatile phase.
    locs = {
        n["node_id"]: {
            "latitude": n.get("latitude"),
            "longitude": n.get("longitude"),
            "city": n.get("city") or n.get("session_city") or "",
            "telescope_model": n.get("telescope_model") or "",
        }
        for n in registry.list_nodes()
    }
    for row in fleet:
        row.update(locs.get(row["node_id"], {}))
    return jsonify({
        "fleet":       fleet,
        "nodes_live":  len(fleet),
        "dark_online": sum(1 for n in fleet if n["online"] and n["is_dark"]),
        "server_time": _now(),
    })


@app.route("/api/v1/network/live-fleet", methods=["GET"])
def api_network_live_fleet():
    """Live fleet activity feed: live fleet + what the network just did on its
    own (mid-night reflows and reflex confirmations). The data behind the member
    'live fleet' view — 'node X clouded out, its targets moved to Y; a candidate
    was auto-confirmed on Z'."""
    night = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    reflows = db.query(
        "SELECT from_node, to_node, target_name, expected_info, outcome, "
        "       created_at FROM reflow_log WHERE night = %s "
        "ORDER BY created_at DESC LIMIT 50", (night,))
    reflexes = db.query(
        "SELECT name, ra_deg, dec_deg, node_ids, created_at FROM interrupts "
        "WHERE reason = 'reflex_confirm' AND created_at > %s "
        "ORDER BY created_at DESC LIMIT 50", (since,))
    for r in reflexes:
        r["node_ids"] = db.loads(r.pop("node_ids", "[]"), [])
        r["source_key"] = str(r.get("name", "")).replace("reflex:", "")
    return jsonify({
        "fleet":       live.fleet_state(),
        "reflows":     reflows,
        "reflex_confirmations": reflexes,
        "server_time": _now(),
    })


@app.route("/api/v1/network/activity", methods=["GET"])
def api_network_activity():
    """Public feed of recent observations, submissions, nodes, and interrupts."""
    limit = min(int(request.args.get("limit", 50)), 200)
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    events = []

    for r in db.query(
        """SELECT node_id, target_name, magnitude, uncertainty, filter,
                  aavso_submitted, received_at
           FROM measurements WHERE received_at > %s
           ORDER BY received_at DESC LIMIT %s""",
        (since, limit),
    ):
        events.append({
            "type": "aavso_submission" if r["aavso_submitted"] else "measurement",
            "at": r["received_at"],
            "node_id": r["node_id"],
            "target": r["target_name"],
            "summary": (
                f"{r['target_name']} {r['magnitude']:.3f}±{r['uncertainty']:.3f} "
                f"{r['filter'] or 'CV'}"
            ),
        })

    for r in db.query(
        """SELECT node_id, city, country, registered_at
           FROM nodes WHERE registered_at > %s
           ORDER BY registered_at DESC LIMIT %s""",
        (since, max(10, limit // 4)),
    ):
        place = ", ".join(p for p in (r.get("city"), r.get("country")) if p)
        events.append({
            "type": "node_joined",
            "at": r["registered_at"],
            "node_id": r["node_id"],
            "summary": f"New node joined{f' in {place}' if place else ''}.",
        })

    for r in db.query(
        """SELECT id, name, reason, created_at, expires_at
           FROM interrupts WHERE created_at > %s
           ORDER BY created_at DESC LIMIT %s""",
        (since, max(10, limit // 4)),
    ):
        events.append({
            "type": "transient_interrupt",
            "at": r["created_at"],
            "target": r["name"],
            "summary": r["reason"] or f"High-priority interrupt for {r['name']}.",
            "expires_at": r["expires_at"],
        })

    events.sort(key=lambda e: e["at"], reverse=True)
    return jsonify({"events": events[:limit], "server_time": _now()})


# ── Site config ────────────────────────────────────────────────────────────────

@app.route("/api/v1/site/config", methods=["GET"])
def api_site_config():
    row = db.query_one("SELECT member_count FROM site_config WHERE id = 1") or {"member_count": 7}
    return jsonify({"member_count": row["member_count"]})


@app.route("/api/v1/site/config", methods=["PATCH"])
@require_admin
def api_site_config_update():
    body = request.get_json(silent=True) or {}
    if "member_count" in body:
        db.execute(
            "UPDATE site_config SET member_count = %s, updated_at = %s WHERE id = 1",
            (int(body["member_count"]), _now()),
        )
    return api_site_config()


# ── Subscribe (public join flow) ───────────────────────────────────────────────

@app.route("/api/v1/subscribe", methods=["POST"])
def api_subscribe():
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "valid email required"}), 400
    source = str(body.get("source") or "tour")[:64]
    equipment = str(body.get("equipment") or "")[:64]

    existing = db.query_one("SELECT id, activation_code FROM subscribers WHERE email = %s", (email,))
    if existing:
        return jsonify({"ok": True, "code": existing["activation_code"], "new": False})

    code = _generate_activation_code()
    db.execute(
        "INSERT INTO subscribers (email, source, equipment, subscribed_at, activation_code, status)"
        " VALUES (%s, %s, %s, %s, %s, 'pending')",
        (email, source, equipment, _now(), code),
    )
    db.execute(
        "UPDATE site_config SET member_count = member_count + 1, updated_at = %s WHERE id = 1",
        (_now(),),
    )
    return jsonify({"ok": True, "code": code, "new": True})


@app.route("/api/v1/admin/subscribers", methods=["GET"])
@require_admin
def api_admin_subscribers():
    rows = db.query(
        "SELECT id, email, source, equipment, subscribed_at, activation_code, status"
        " FROM subscribers ORDER BY subscribed_at DESC"
    )
    return jsonify({"subscribers": rows, "total": len(rows)})


@app.route("/api/v1/admin/subscribers/<int:sub_id>/status", methods=["PATCH"])
@require_admin
def api_admin_subscriber_status(sub_id):
    body = request.get_json(force=True, silent=True) or {}
    status = str(body.get("status") or "").strip()
    if status not in ("pending", "sent", "onboarded"):
        return jsonify({"error": "status must be pending, sent, or onboarded"}), 400
    db.execute("UPDATE subscribers SET status = %s WHERE id = %s", (status, sub_id))
    return jsonify({"ok": True})


# ── Admin operations ───────────────────────────────────────────────────────────

@app.route("/api/v1/admin/ingest", methods=["POST"])
@require_admin
def api_admin_ingest():
    result = alerts.ingest_all(_config)
    scoring.score_all(_config)
    return jsonify(result)


@app.route("/api/v1/admin/replan", methods=["POST"])
@require_admin
def api_admin_replan():
    scored = scoring.score_all(_config)
    plans = scheduler.generate_all_plans(_config)
    return jsonify({"scored_pairs": scored, "plans_generated": plans})


@app.route("/api/v1/admin/tuning", methods=["GET"])
@require_admin
def api_admin_tuning():
    """Active observability weights plus recent auto-tuning history."""
    history = db.query(
        """SELECT id, changed_at, old_weights, new_weights, rationale,
                  model, applied
           FROM weight_history ORDER BY changed_at DESC LIMIT 20""")
    for row in history:
        row["old_weights"] = db.loads(row["old_weights"], {})
        row["new_weights"] = db.loads(row["new_weights"], {})
    return jsonify({
        "active_weights": tuning.active_obs_weights(_config),
        "history": history,
    })


@app.route("/api/v1/admin/tuning/rollback", methods=["POST"])
@require_admin
def api_admin_tuning_rollback():
    """Restore the previous weights from the audit log (manual safety valve)."""
    last = db.query_one(
        "SELECT old_weights, rationale FROM weight_history "
        "ORDER BY changed_at DESC LIMIT 1")
    if not last:
        return jsonify({"error": "no tuning history to roll back"}), 404
    restored = db.loads(last["old_weights"], {})
    tuning.restore_weights(
        restored, f"manual rollback (was: {last.get('rationale','')})", _config)
    return jsonify({"restored_weights": tuning.active_obs_weights(_config)})


@app.route("/api/v1/admin/sky-quality", methods=["GET"])
@require_admin
def api_admin_sky_quality():
    """Per-node sky brightness stats. ?days=N (default 30)."""
    days = float(request.args.get("days", 30))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.query(
        """SELECT m.node_id, n.city, n.country, n.light_pollution_mpsas AS declared_mpsas,
                  COUNT(*) AS n_frames,
                  AVG(m.sky_mag) AS mean_sky_mag,
                  MIN(m.sky_mag) AS best_sky_mag,
                  MAX(m.sky_mag) AS worst_sky_mag,
                  MAX(m.received_at) AS last_seen
             FROM measurements m
             LEFT JOIN nodes n USING (node_id)
            WHERE m.sky_mag IS NOT NULL AND m.received_at > %s
            GROUP BY m.node_id, n.city, n.country, n.light_pollution_mpsas
            ORDER BY mean_sky_mag DESC NULLS LAST""",
        (cutoff,),
    )
    for r in rows:
        for k in ("mean_sky_mag", "best_sky_mag", "worst_sky_mag"):
            if r.get(k) is not None:
                r[k] = round(float(r[k]), 2)
    return jsonify({"days": days, "nodes": rows})


@app.route("/api/v1/admin/patrol", methods=["GET"])
@require_admin
def api_admin_patrol():
    """Recent patrol detections across all nodes. ?hours=N (default 48)."""
    hours = float(request.args.get("hours", 48))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = db.query(
        """SELECT pd.id, pd.node_id, pd.target_name, pd.bjd, pd.ra_deg, pd.dec_deg,
                  pd.est_mag, pd.catalog_mag, pd.delta_mag, pd.alert_type,
                  pd.status, pd.detected_at, n.owner_name
             FROM patrol_detections pd
             LEFT JOIN nodes n USING (node_id)
            WHERE pd.detected_at > %s
            ORDER BY pd.detected_at DESC LIMIT 200""",
        (cutoff,),
    )
    return jsonify({"n": len(rows), "hours": hours, "detections": rows})


@app.route("/api/v1/admin/candidates", methods=["GET"])
@require_admin
def api_admin_candidates():
    """Discovery candidates. ?state=candidate|detected|crossmatching|confirmed|
    rejected|known_vsx|known_tns|open|all (default: candidate — human review queue)."""
    state = request.args.get("state", "candidate")
    if state == "all":
        rows = db.query(
            "SELECT * FROM discovery_candidates ORDER BY updated_at DESC LIMIT 200")
    elif state == "open":
        rows = db.query(
            "SELECT * FROM discovery_candidates WHERE state IN %s "
            "ORDER BY updated_at DESC LIMIT 200", (survey.OPEN_STATES,))
    else:
        rows = db.query(
            "SELECT * FROM discovery_candidates WHERE state = %s "
            "ORDER BY updated_at DESC LIMIT 200", (state,))
    return jsonify({"count": len(rows), "state": state, "candidates": rows,
                    "survey": survey.stats()})


@app.route("/api/v1/admin/candidates/<int:cand_id>", methods=["PATCH"])
@require_admin
def api_admin_candidate_update(cand_id: int):
    """Human verdict on a discovery candidate.

    Body: {"action": "confirm"|"reject", "target_type": "VAR|SN|...",
           "note": "..."} — confirm creates a target CHORUS schedules on the
    next replan cycle.
    """
    from cloud import crossmatch as _cm
    body = request.get_json(force=True, silent=True) or {}
    action = str(body.get("action") or "").lower()
    if action == "confirm":
        result = _cm.confirm_candidate(
            cand_id, target_type=str(body.get("target_type") or ""),
            note=str(body.get("note") or ""))
        if result is None:
            return jsonify({"error": "candidate not found or not open"}), 404
        return jsonify({"ok": True, **result})
    if action == "reject":
        if not _cm.reject_candidate(cand_id, note=str(body.get("note") or "")):
            return jsonify({"error": "candidate not found"}), 404
        return jsonify({"ok": True, "candidate_id": cand_id})
    return jsonify({"error": "action must be confirm or reject"}), 400


@app.route("/api/v1/admin/asteroid-candidates", methods=["GET"])
@require_admin
def api_admin_asteroid_candidates():
    """Moving-object tracklets. ?state=linked|candidate|known_skybot|
    confirmed|rejected|all (default: candidate — human review queue)."""
    from cloud import moving_objects
    state = request.args.get("state", "candidate")
    if state == "all":
        rows = db.query(
            "SELECT * FROM asteroid_candidates ORDER BY updated_at DESC LIMIT 200")
    else:
        rows = db.query(
            "SELECT * FROM asteroid_candidates WHERE state = %s "
            "ORDER BY updated_at DESC LIMIT 200", (state,))
    return jsonify({"count": len(rows), "state": state, "candidates": rows,
                    "moving_objects": moving_objects.stats()})


@app.route("/api/v1/admin/asteroid-candidates/<int:cand_id>", methods=["PATCH"])
@require_admin
def api_admin_asteroid_candidate_update(cand_id: int):
    """Human verdict on a moving-object tracklet.

    Body: {"action": "confirm"|"reject", "note": "..."} — confirm writes an
    MPC ADES astrometry report to cloud_data/mpc_reports for manual
    submission (see GET /api/v1/mpc-files).
    """
    from cloud import moving_objects
    body = request.get_json(force=True, silent=True) or {}
    action = str(body.get("action") or "").lower()
    if action == "confirm":
        result = moving_objects.confirm_candidate(
            cand_id, _config, note=str(body.get("note") or ""))
        if result is None:
            return jsonify({"error": "candidate not found or not open"}), 404
        return jsonify({"ok": True, **result})
    if action == "reject":
        if not moving_objects.reject_candidate(cand_id, note=str(body.get("note") or "")):
            return jsonify({"error": "candidate not found"}), 404
        return jsonify({"ok": True, "candidate_id": cand_id})
    return jsonify({"error": "action must be confirm or reject"}), 400


@app.route("/api/v1/admin/incidents", methods=["GET"])
@require_admin
def api_admin_incidents():
    """List structured incidents. ?status=open|investigating|resolved|all (default: open+investigating)."""
    status_filter = request.args.get("status", "active")
    if status_filter == "all":
        rows = db.query(
            "SELECT * FROM incidents ORDER BY opened_at DESC LIMIT 200"
        )
    elif status_filter == "resolved":
        rows = db.query(
            "SELECT * FROM incidents WHERE status='resolved' ORDER BY resolved_at DESC LIMIT 200"
        )
    else:
        rows = db.query(
            "SELECT * FROM incidents WHERE status IN ('open','investigating') ORDER BY opened_at DESC"
        )
    return jsonify({"incidents": rows, "count": len(rows)})


@app.route("/api/v1/admin/incidents/<int:incident_id>", methods=["PATCH"])
@require_admin
def api_admin_incident_update(incident_id: int):
    """
    Update a structured incident.

    Body fields (all optional):
        status          open | investigating | resolved
        root_cause      weather | hardware | software | optics | unknown
        resolution_note free text
        resolver        name/email of person resolving
    """
    body = request.get_json(force=True, silent=True) or {}
    allowed = {"status", "root_cause", "resolution_note", "resolver"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return jsonify({"error": "no valid fields to update"}), 400

    now = _now()
    set_clauses = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [now, incident_id]
    db.execute(
        f"UPDATE incidents SET {set_clauses}, updated_at = %s WHERE id = %s",
        values,
    )
    if updates.get("status") == "resolved" and "resolved_at" not in updates:
        db.execute("UPDATE incidents SET resolved_at = %s WHERE id = %s AND resolved_at IS NULL",
                   (now, incident_id))

    row = db.query_one("SELECT * FROM incidents WHERE id = %s", (incident_id,))
    return jsonify({"incident": row})


@app.route("/api/v1/health", methods=["GET"])
def api_health():
    try:
        db.query_one("SELECT 1 AS ok")
        db_ok = True
    except Exception:
        db_ok = False
    code = 200 if db_ok else 503
    return jsonify({"ok": db_ok, "db": db_ok, "server_time": _now()}), code


@app.route("/api/v1/weather", methods=["GET"])
def api_weather():
    """
    Astronomy weather forecast for a lat/lon.

    Query params: lat, lon (required)
    Returns 7timer ASTRO forecast: cloud cover, seeing, transparency per 3-h slot.
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat and lon query params required"}), 400

    forecast = fetch_astronomy_weather(lat, lon)
    if forecast is None:
        return jsonify({"error": "weather data unavailable"}), 503

    # Serialise datetime objects to ISO strings
    payload = {
        "source": "7timer_astro",
        "latitude": lat,
        "longitude": lon,
        "slots": [
            {
                "time": t.isoformat(),
                "cloud_cover_pct": forecast["cloud_cover"][i],
                "seeing": forecast["seeing"][i],
                "transparency": forecast["transparency"][i],
                "lifted_index": forecast["lifted_index"][i],
                "wind_kmh": forecast["wind_kmh"][i],
                "humidity_pct": forecast["humidity"][i],
            }
            for i, t in enumerate(forecast["times"])
        ],
    }
    return jsonify(payload)


@app.route("/api/v1/light-pollution", methods=["GET"])
def api_light_pollution():
    """
    Sky brightness for a lat/lon.

    Query params: lat, lon (required)
    Returns mpsas, bortle, and the data source used.
    Cached server-side for 7 days per location.
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat and lon query params required"}), 400

    lp_api_key = _config.get("light_pollution", {}).get("api_key", "")
    result = fetch_light_pollution_detail(lat, lon, lp_api_key)
    return jsonify({
        "latitude": lat,
        "longitude": lon,
        "mpsas": result["mpsas"],
        "bortle": result["bortle"],
        "source": result["source"],
        "radiance_nw_cm2_sr": result.get("radiance"),
    })


@app.route("/api/v1/telescopes", methods=["GET"])
def api_telescopes():
    """Public telescope spec catalog — powers the app's model picker.

    Each entry includes the physical specs plus the derived parameters
    (pixel scale, FOV, magnitude limits) so the app can show a confirmation
    card without recomputing the physics."""
    from src import telescope_specs
    return jsonify({"telescopes": telescope_specs.catalog_list()})


@app.errorhandler(Exception)
def handle_unhandled_error(exc):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return jsonify({"error": "internal server error"}), 500


# ── Member auth ────────────────────────────────────────────────────────────────

@app.route("/api/v1/auth/register", methods=["POST"])
def api_auth_register():
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = auth.register(
            body.get("email", ""),
            body.get("password", ""),
            body.get("display_name", ""),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/v1/admin/calibration/models", methods=["GET"])
@require_admin
def api_admin_calibration_models():
    rows = db.query(
        "SELECT * FROM photometric_models ORDER BY created_at DESC LIMIT 500")
    for row in rows:
        row["validation"] = db.loads(row.get("validation"), {})
    return jsonify({"models": rows})


@app.route("/api/v1/admin/calibration/models/<model_version>/rollback", methods=["POST"])
@require_admin
def api_admin_calibration_rollback(model_version):
    if not calibration.rollback(model_version):
        return jsonify({"error": "model not found"}), 404
    return jsonify({"ok": True, "model_version": model_version})


@app.route("/api/v1/auth/login", methods=["POST"])
def api_auth_login():
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = auth.login(body.get("email", ""), body.get("password", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 401
    return jsonify(result)


@app.route("/api/v1/auth/logout", methods=["POST"])
def api_auth_logout():
    """Revoke only the calling device's session -- other signed-in devices
    (e.g. desktop app while phone is also signed in) are unaffected."""
    token = auth._extract_token()
    if token:
        auth.logout(token)
    return jsonify({"ok": True})


# ── Member profile ─────────────────────────────────────────────────────────────

@app.route("/api/v1/me", methods=["GET"])
@auth.require_member
def api_me(user):
    member = db.query_one(
        "SELECT display_name, country FROM members WHERE user_id = %s",
        (user["user_id"],),
    )
    return jsonify({
        "user_id":      user["user_id"],
        "email":        user["email"],
        "role":         user["role"],
        "display_name": (member or {}).get("display_name", ""),
        "country":      (member or {}).get("country", ""),
        "created_at":   user["created_at"],
        "last_login":   user["last_login"],
    })


@app.route("/api/v1/me/nodes", methods=["GET"])
@auth.require_member
def api_me_nodes(user):
    """All nodes this member has claimed."""
    rows = db.query(
        """SELECT n.node_id, n.telescope_model, n.telescope_name, n.city, n.country, n.status,
                  n.last_heartbeat, n.last_conditions, n.portable, n.vacation_until,
                  n.session_city, n.session_site_name, n.previous_locations,
                  nm.claimed_at, nm.display_name
           FROM nodes n
           JOIN node_members nm ON nm.node_id = n.node_id
           WHERE nm.user_id = %s""",
        (user["user_id"],),
    )
    for r in rows:
        r["online"] = registry.is_online(r)
        r["portable"] = bool(r.get("portable"))
        r["previous_locations"] = db.loads(r.get("previous_locations"), [])
        r["conditions"] = db.loads(r.get("last_conditions"), {})
        r.pop("last_conditions", None)
    return jsonify({"nodes": rows})


@app.route("/api/v1/me/nodes/<node_id>/live", methods=["GET"])
@auth.require_member
def api_me_node_live(user, node_id):
    """Live phase of one of the member's own nodes (live fleet view)."""
    owns = db.query_one(
        "SELECT 1 FROM node_members WHERE node_id = %s AND user_id = %s",
        (node_id, user["user_id"]),
    )
    if owns is None:
        return jsonify({"error": "not your node"}), 403
    state = live.node_live(node_id)
    if state is None:
        return jsonify({"live": None, "message": "node has not reported live state"}), 200
    return jsonify({"live": state})


@app.route("/api/v1/me/highlights", methods=["GET"])
@auth.require_member
def api_me_highlights(user):
    """Notable observation highlights for the authenticated member. ?limit=N (default 50)."""
    limit = min(int(request.args.get("limit", 50)), 200)
    rows = db.query(
        """SELECT id, node_id, measurement_id, target_name, target_type,
                  bjd, magnitude, headline, detail, created_at, read_at
             FROM member_highlights
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s""",
        (user["user_id"], limit),
    )
    unread = sum(1 for r in rows if not r.get("read_at"))
    return jsonify({"highlights": rows, "unread": unread})


@app.route("/api/v1/me/highlights/<int:highlight_id>/read", methods=["POST"])
@auth.require_member
def api_me_highlight_read(user, highlight_id: int):
    """Mark a highlight as read."""
    db.execute(
        "UPDATE member_highlights SET read_at = %s WHERE id = %s AND user_id = %s",
        (_now(), highlight_id, user["user_id"]),
    )
    return jsonify({"ok": True})


@app.route("/api/v1/me/nodes/<node_id>", methods=["POST"])
@auth.require_member
def api_me_claim_node(user, node_id):
    """
    Claim a node by presenting its api_key.
    The member must know the node_id and api_key returned at registration.
    """
    body = request.get_json(force=True, silent=True) or {}
    node = registry.authenticate(node_id, body.get("api_key", ""))
    if node is None:
        return jsonify({"error": "invalid node credentials"}), 401
    if not db.query_one(
        "SELECT 1 FROM node_members WHERE node_id = %s AND user_id = %s",
        (node_id, user["user_id"]),
    ):
        db.execute(
            "INSERT INTO node_members (node_id, user_id, claimed_at) VALUES (%s,%s,%s)",
            (node_id, user["user_id"], _now()),
        )
        logger.info("Node %s claimed by member %s", node_id, user["user_id"])
    return jsonify({"ok": True, "node_id": node_id})


@app.route("/api/v1/me/nodes/attach", methods=["POST"])
@auth.require_member
def api_me_attach_node(user):
    """Link a telescope to the signed-in member (replaces activation codes).

    Body (same fields the old activation-code form collected):
      latitude, longitude, location_name / owner_name,
      telescope_model, telescope_display_name, telescope_specs, portable

    Optionally pass existing node_id + api_key to claim an already-registered
    agent without creating a new cloud row.

    Returns {node_id, api_key} for the desktop app to install on the local agent.
    """
    body = request.get_json(force=True, silent=True) or {}

    existing_id = str(body.get("node_id") or "").strip()
    existing_key = str(body.get("api_key") or "").strip()
    if existing_id and existing_key:
        node = registry.authenticate(existing_id, existing_key)
        if node is None:
            return jsonify({"error": "invalid node credentials"}), 401
        display_name = str(body.get("telescope_display_name") or "").strip()[:80]
        if not db.query_one(
            "SELECT 1 FROM node_members WHERE node_id = %s AND user_id = %s",
            (existing_id, user["user_id"]),
        ):
            db.execute(
                "INSERT INTO node_members (node_id, user_id, claimed_at, display_name)"
                " VALUES (%s,%s,%s,%s)",
                (existing_id, user["user_id"], _now(), display_name),
            )
        elif display_name:
            db.execute(
                "UPDATE node_members SET display_name = %s"
                " WHERE node_id = %s AND user_id = %s",
                (display_name, existing_id, user["user_id"]),
            )
        logger.info("Member %s attached existing node %s", user["user_id"], existing_id)
        return jsonify({"node_id": existing_id, "api_key": existing_key, "linked": True})

    try:
        lat = float(body.get("latitude"))
        lon = float(body.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "latitude and longitude are required"}), 400
    if lat == 0.0 and lon == 0.0:
        return jsonify({"error": "latitude and longitude are required"}), 400

    location_name = str(
        body.get("location_name") or body.get("owner_name") or ""
    ).strip()
    telescope_model = str(body.get("telescope_model") or "").strip() or "Unknown"
    telescope_display_name = str(body.get("telescope_display_name") or "").strip()[:80]
    portable = bool(body.get("portable"))

    info: dict = {
        "latitude": lat,
        "longitude": lon,
        "owner_name": location_name,
        "telescope_model": telescope_model,
        "telescope_name": telescope_display_name or telescope_model,
        "portable": portable,
    }

    specs = dict(body.get("telescope_specs") or {})
    if telescope_model:
        try:
            from src import telescope_specs as _ts
            spec = _ts.lookup(telescope_model)
            if spec is not None:
                info["telescope_model"] = spec.display_name
                merged = _ts.derive_params(spec)
                merged.update({k: v for k, v in specs.items() if v not in (None, "")})
                specs = merged
        except Exception as exc:
            logger.debug("telescope_specs lookup skipped: %s", exc)
    for key, val in specs.items():
        if val in (None, "") or info.get(key) not in (None, "", 0, 0.0):
            continue
        info[key] = json.dumps(val) if key == "filter_set" and not isinstance(val, str) else val

    try:
        creds = registry.register_node(
            info, _config.get("light_pollution", {}).get("api_key", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not db.query_one(
        "SELECT 1 FROM node_members WHERE node_id = %s AND user_id = %s",
        (creds["node_id"], user["user_id"]),
    ):
        db.execute(
            "INSERT INTO node_members (node_id, user_id, claimed_at, display_name)"
            " VALUES (%s,%s,%s,%s)",
            (creds["node_id"], user["user_id"], _now(), telescope_display_name),
        )
    logger.info("Member %s attached new node %s (%s)",
                user["user_id"], creds["node_id"], telescope_model)
    return jsonify({**creds, "linked": True})


def _assert_owns_node(user_id: str, node_id: str) -> bool:
    return bool(db.query_one(
        "SELECT 1 FROM node_members WHERE node_id = %s AND user_id = %s",
        (node_id, user_id),
    ))


@app.route("/api/v1/me/nodes/<node_id>/session", methods=["POST"])
@auth.require_member
def api_me_start_session(user, node_id):
    """Start a portable node's observing session for tonight.

    Body: {lat, lon, city, site_name}
    Returns: {mpsas, bortle} for the session location.
    """
    if not _assert_owns_node(user["user_id"], node_id):
        return jsonify({"error": "node not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    try:
        lat = float(body["lat"])
        lon = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon required"}), 400
    city = str(body.get("city") or "").strip()
    site_name = str(body.get("site_name") or "").strip()
    try:
        result = registry.start_session(
            node_id, lat, lon, city, site_name,
            _config.get("light_pollution", {}).get("api_key", ""),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/v1/me/nodes/<node_id>/session", methods=["DELETE"])
@auth.require_member
def api_me_end_session(user, node_id):
    """Manually end a portable node's session (sets it back to sleeping)."""
    if not _assert_owns_node(user["user_id"], node_id):
        return jsonify({"error": "node not found"}), 404
    registry.end_session(node_id)
    return jsonify({"ok": True})


@app.route("/api/v1/me/nodes/<node_id>/vacation", methods=["PUT"])
@auth.require_member
def api_me_set_vacation(user, node_id):
    """Put a node on vacation until *until_date* (ISO date 'YYYY-MM-DD').

    Body: {until_date: "YYYY-MM-DD"}
    """
    if not _assert_owns_node(user["user_id"], node_id):
        return jsonify({"error": "node not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    until_date = str(body.get("until_date") or "").strip()
    if not until_date:
        return jsonify({"error": "until_date required (YYYY-MM-DD)"}), 400
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", until_date):
        return jsonify({"error": "until_date must be YYYY-MM-DD"}), 400
    registry.set_vacation(node_id, until_date)
    return jsonify({"ok": True, "vacation_until": until_date})


@app.route("/api/v1/me/nodes/<node_id>/vacation", methods=["DELETE"])
@auth.require_member
def api_me_cancel_vacation(user, node_id):
    """Cancel an active vacation early."""
    if not _assert_owns_node(user["user_id"], node_id):
        return jsonify({"error": "node not found"}), 404
    registry.clear_vacation(node_id)
    return jsonify({"ok": True})


@app.route("/api/v1/me/nodes/<node_id>", methods=["PUT"])
@auth.require_member
def api_me_update_node(user, node_id):
    """Update member-specific settings for a claimed node (e.g. display name)."""
    if not _assert_owns_node(user["user_id"], node_id):
        return jsonify({"error": "node not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    if "display_name" in body:
        display_name = str(body.get("display_name") or "").strip()[:80]
        db.execute(
            "UPDATE node_members SET display_name = %s WHERE node_id = %s AND user_id = %s",
            (display_name, node_id, user["user_id"]),
        )
    return jsonify({"ok": True, "node_id": node_id})


@app.route("/api/v1/me/nodes/<node_id>", methods=["DELETE"])
@auth.require_member
def api_me_disconnect_node(user, node_id):
    """Remove this member's claim on a node (disconnect it from their account)."""
    if not _assert_owns_node(user["user_id"], node_id):
        return jsonify({"error": "node not found"}), 404
    db.execute(
        "DELETE FROM node_members WHERE node_id = %s AND user_id = %s",
        (node_id, user["user_id"]),
    )
    logger.info("Node %s disconnected by member %s", node_id, user["user_id"])
    return jsonify({"ok": True})


@app.route("/api/v1/sky-quality", methods=["GET"])
def api_sky_quality():
    """Return light pollution data for a lat/lon (used by the Start Tonight sheet).

    Query params: lat, lon
    Returns: {mpsas, bortle}
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat and lon required"}), 400
    from cloud.conditions import fetch_light_pollution as _lp
    mpsas, bortle = _lp(lat, lon, _config.get("light_pollution", {}).get("api_key", ""))
    return jsonify({"mpsas": mpsas, "bortle": bortle})


@app.route("/api/v1/me/observations", methods=["GET"])
@auth.require_member
def api_me_observations(user):
    """Observations from all nodes owned by this member."""
    days = min(int(request.args.get("days", 90)), 365)
    limit = min(int(request.args.get("limit", 200)), 100_000)

    node_ids = [r["node_id"] for r in db.query(
        "SELECT node_id FROM node_members WHERE user_id = %s", (user["user_id"],))]
    if not node_ids:
        return jsonify({"observations": [], "total": 0})

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    placeholders = ",".join(["%s"] * len(node_ids))
    rows = db.query(
        f"""SELECT node_id, target_name, bjd, magnitude, uncertainty, filter,
                   quality_flag, aavso_submitted, received_at
            FROM measurements
            WHERE node_id IN ({placeholders}) AND received_at >= %s
            ORDER BY bjd DESC LIMIT %s""",
        (*node_ids, cutoff, limit),
    )
    return jsonify({"observations": rows, "total": len(rows)})


@app.route("/api/v1/me/stats", methods=["GET"])
@auth.require_member
def api_me_stats(user):
    """Cumulative statistics for all nodes this member owns."""
    node_ids = [r["node_id"] for r in db.query(
        "SELECT node_id FROM node_members WHERE user_id = %s", (user["user_id"],))]
    if not node_ids:
        return jsonify({
            "total_observations": 0, "aavso_submitted": 0,
            "targets_observed": 0, "clear_nights": 0, "node_count": 0,
        })

    placeholders = ",".join(["%s"] * len(node_ids))
    totals = db.query_one(
        f"""SELECT COUNT(*) AS total,
                   SUM(aavso_submitted) AS submitted,
                   COUNT(DISTINCT target_name) AS targets
            FROM measurements WHERE node_id IN ({placeholders})""",
        tuple(node_ids),
    ) or {}
    clear = db.query_one(
        f"""SELECT SUM(CASE WHEN n_observations > 0 THEN 1 ELSE 0 END) AS clear_nights
            FROM night_summaries WHERE node_id IN ({placeholders})""",
        tuple(node_ids),
    ) or {}
    contrib = db.query_one(
        """SELECT COUNT(*) AS frames,
                  COALESCE(SUM(n_sources), 0) AS stars
             FROM contributions WHERE user_id = %s AND status = 'done'""",
        (user["user_id"],)) or {}
    placeholders_all = ",".join(["%s"] * len(node_ids))
    survey_stars = db.query_one(
        f"""SELECT COUNT(*) AS n FROM survey_measurements
            WHERE node_id IN ({placeholders_all})""",
        tuple(node_ids)) or {}
    discoveries = db.query_one(
        """SELECT COUNT(*) AS n FROM discovery_candidates
           WHERE state = 'confirmed' AND node_ids::text LIKE ANY(%s)""",
        (["%" + nid + "%" for nid in node_ids],)) or {}
    return jsonify({
        "total_observations": totals.get("total", 0) or 0,
        "aavso_submitted":    int(totals.get("submitted", 0) or 0),
        "targets_observed":   totals.get("targets", 0) or 0,
        "clear_nights":       int(clear.get("clear_nights", 0) or 0),
        "node_count":         len(node_ids),
        "frames_contributed": int(contrib.get("frames", 0) or 0),
        "survey_stars_measured": int(survey_stars.get("n", 0) or 0),
        "discoveries_touched":   int(discoveries.get("n", 0) or 0),
    })


@app.route("/api/v1/me/timeline", methods=["GET"])
@auth.require_member
def api_me_timeline(user):
    """Tonight's planned observing timeline across the member's nodes."""
    node_ids = [r["node_id"] for r in db.query(
        "SELECT node_id FROM node_members WHERE user_id = %s", (user["user_id"],))]
    if not node_ids:
        return jsonify({"items": [], "plans": []})

    placeholders = ",".join(["%s"] * len(node_ids))
    rows = db.query(
        f"""SELECT p.node_id, p.night, p.generated_at, p.plan_json,
                   n.telescope_model, n.city, n.country, n.status,
                   n.last_heartbeat, n.utc_offset_hours
            FROM plans p
            JOIN nodes n ON n.node_id = p.node_id
            WHERE p.node_id IN ({placeholders}) AND p.status = 'current'
            ORDER BY p.generated_at DESC""",
        tuple(node_ids),
    )
    items = []
    plans = []
    now_local = datetime.now(timezone.utc)
    for r in rows:
        plan = db.loads(r["plan_json"], {})
        node_meta = {
            "node_id": r["node_id"],
            "telescope_model": r["telescope_model"],
            "city": r["city"],
            "country": r["country"],
            "status": r["status"],
            "online": registry.is_online(r),
        }
        plans.append({
            "node": node_meta,
            "night": r["night"],
            "generated_at": r["generated_at"],
            "n_items": len(plan.get("items", [])),
        })
        for i, item in enumerate(plan.get("items", [])):
            exp_min = float(item.get("expDur") or 0) * int(item.get("expCount") or 0) / 60.0
            state = "planned"
            try:
                hh, mm = [int(part) for part in str(item.get("startTime", "0:0")).split(":")[:2]]
                local_day = datetime.strptime(r["night"], "%Y-%m-%d")
                if hh < 12:
                    local_day += timedelta(days=1)
                start_local = local_day.replace(hour=hh, minute=mm)
                end_local = start_local + timedelta(minutes=max(exp_min, 5.0))
                offset_h = float(r.get("utc_offset_hours") or 0.0)
                # Compute node's current local wall time (as naive datetime) for window comparison.
                # plan times are expressed in the node's local civil time.
                utc_now = datetime.now(timezone.utc)
                local_now = (utc_now + timedelta(hours=offset_h)).replace(tzinfo=None)
                if start_local <= local_now <= end_local:
                    state = "observing"
                elif local_now > end_local:
                    state = "complete"
            except Exception:
                state = "planned"
            items.append({
                **item,
                "node": node_meta,
                "sequence": i + 1,
                "estimated_minutes": round(exp_min, 1),
                "state": state,
            })
    items.sort(key=lambda item: (item.get("startTime") or "", item["node"]["node_id"]))
    return jsonify({"items": items, "plans": plans, "server_time": now_local.isoformat()})


@app.route("/api/v1/me/nights", methods=["GET"])
@auth.require_member
def api_me_nights(user):
    """Night summaries for this member's nodes, most recent first."""
    limit = min(int(request.args.get("limit", 30)), 90)
    node_ids = [r["node_id"] for r in db.query(
        "SELECT node_id FROM node_members WHERE user_id = %s", (user["user_id"],))]
    if not node_ids:
        return jsonify({"nights": []})

    placeholders = ",".join(["%s"] * len(node_ids))
    rows = db.query(
        f"""SELECT node_id, night, n_targets, n_observations, n_submitted,
                   summary_json, generated_at
            FROM night_summaries
            WHERE node_id IN ({placeholders})
            ORDER BY night DESC LIMIT %s""",
        (*node_ids, limit),
    )
    for r in rows:
        summary = db.loads(r.pop("summary_json"), {})
        r["targets"] = summary.get("targets", {})
        r["receipt"] = summary.get("receipt", {})
    return jsonify({"nights": rows})


@app.route("/api/v1/me/incidents", methods=["GET"])
@auth.require_member
def api_me_incidents(user):
    """Recent reliability incidents for nodes this member owns."""
    limit = min(int(request.args.get("limit", 50)), 200)
    node_ids = [r["node_id"] for r in db.query(
        "SELECT node_id FROM node_members WHERE user_id = %s", (user["user_id"],))]
    if not node_ids:
        return jsonify({"incidents": []})
    placeholders = ",".join(["%s"] * len(node_ids))
    rows = db.query(
        f"""SELECT id, node_id, incident_type, severity, target_name,
                   measurement_id, detail, occurred_at, resolved_at
            FROM reliability_incidents
            WHERE node_id IN ({placeholders})
            ORDER BY occurred_at DESC LIMIT %s""",
        (*node_ids, limit),
    )
    for r in rows:
        r["detail"] = db.loads(r["detail"], {})
        r["resolved"] = bool(r.get("resolved_at"))
    return jsonify({"incidents": rows})


@app.route("/api/v1/me/notifications", methods=["GET"])
@auth.require_member
def api_me_notifications(user):
    limit = min(int(request.args.get("limit", 50)), 200)
    rows = db.query(
        """SELECT id, type, payload, sent_at, read_at
           FROM notifications WHERE user_id = %s ORDER BY sent_at DESC LIMIT %s""",
        (user["user_id"], limit),
    )
    for r in rows:
        r["payload"] = db.loads(r["payload"], {})
    unread = sum(1 for r in rows if r["read_at"] is None)
    return jsonify({"notifications": rows, "unread": unread})


@app.route("/api/v1/me/notifications/<int:notif_id>/read", methods=["POST"])
@auth.require_member
def api_me_notification_read(user, notif_id):
    db.execute(
        "UPDATE notifications SET read_at = %s WHERE id = %s AND user_id = %s",
        (_now(), notif_id, user["user_id"]),
    )
    return jsonify({"ok": True})


@app.route("/api/v1/me/activation-code", methods=["POST"])
@auth.require_member
def api_me_generate_activation_code(user):
    """Retired. Use POST /api/v1/me/nodes/attach instead."""
    return jsonify({
        "error": "activation codes are retired — use Connect telescope in the app "
                 "(POST /api/v1/me/nodes/attach)",
    }), 410


# ── Open contribution: browser FITS uploads ───────────────────────────────────

def _ensure_contributor_node(user) -> str:
    """The member's tier-0 virtual node (created on first contribution).

    Direct insert rather than registry.register_node: virtual nodes have no
    real location (they're never scheduled), so the lat/lon validation and
    light-pollution lookup would only add noise and API calls."""
    row = db.query_one(
        "SELECT n.node_id FROM nodes n JOIN node_members nm USING (node_id) "
        "WHERE nm.user_id = %s AND n.status = 'contributor' LIMIT 1",
        (user["user_id"],))
    if row:
        return row["node_id"]
    node_id = f"node_c{secrets.token_hex(4)}"
    now = _now()
    db.execute(
        "INSERT INTO nodes (node_id, api_key, owner_name, latitude, longitude, "
        " tier, mount_type, telescope_model, status, registered_at, last_heartbeat) "
        "VALUES (%s,%s,%s,0,0,0,'none','Contributed frames','contributor',%s,%s)",
        (node_id, secrets.token_urlsafe(32),
         str(user.get("display_name") or ""), now, now))
    db.execute(
        "INSERT INTO node_members (node_id, user_id, claimed_at) VALUES (%s,%s,%s)",
        (node_id, user["user_id"], now))
    logger.info("Contributor node %s created for member %s",
                node_id, user["user_id"])
    return node_id


def _fits_header_probe(raw: bytes) -> dict:
    """Header sanity checks without loading pixels: WCS present, LIGHT frame,
    plausible DATE-OBS."""
    import io
    from astropy.io import fits as _fits
    with _fits.open(io.BytesIO(raw), memmap=False,
                    ignore_missing_simple=True) as hdul:
        hdr = hdul[0].header
        has_wcs = ("CRVAL1" in hdr and "CRVAL2" in hdr
                   and ("CD1_1" in hdr or "CDELT1" in hdr))
        image_type = str(hdr.get("IMAGETYP", "LIGHT")).strip().upper()
        date_obs = str(hdr.get("DATE-OBS", ""))
    return {"wcs": has_wcs,
            "light": image_type in ("LIGHT", "LIGHT FRAME", ""),
            "date_obs": date_obs}


@app.route("/api/v1/me/contributions", methods=["POST"])
@auth.require_member
def api_me_contribute(user):
    """Drag-and-drop FITS contribution: anyone's existing images become
    survey science. The cloud solver adds a WCS when the frame lacks one, so a
    plain LIGHT frame from any camera is accepted — universal frame ingestion."""
    import hashlib
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "no file in upload"}), 400
    raw = f.read()
    if len(raw) > 32 * 1024 * 1024:
        return jsonify({"error": "file exceeds 32 MB"}), 400
    if len(raw) < 2880:
        return jsonify({"error": "not a FITS file"}), 400

    daily_cap = int((_config.get("survey") or {}).get("contrib_daily_cap", 200))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n_today = (db.query_one(
        "SELECT COUNT(*) AS n FROM contributions "
        "WHERE user_id = %s AND created_at >= %s",
        (user["user_id"], today)) or {}).get("n", 0)
    if n_today >= daily_cap:
        return jsonify({"error": f"daily contribution cap ({daily_cap}) reached"}), 429

    try:
        probe = _fits_header_probe(raw)
    except Exception:
        return jsonify({"error": "could not parse FITS header"}), 400
    if not probe["light"]:
        return jsonify({"error": "only LIGHT frames carry survey science"}), 400
    # No WCS is fine now: the ingest worker plate-solves frames on the solver
    # service before extraction. wcs_present just records what arrived.

    sha256 = hashlib.sha256(raw).hexdigest()
    if db.query_one("SELECT id FROM contributions WHERE sha256 = %s", (sha256,)):
        return jsonify({"error": "this frame was already contributed"}), 409

    from pathlib import Path
    import re as _re
    safe_name = _re.sub(r"[^A-Za-z0-9_.\-]", "_", f.filename or "frame.fits")
    if not safe_name.lower().endswith((".fits", ".fit")):
        safe_name += ".fits"
    dest_dir = Path((_config.get("survey") or {})
                    .get("contrib_dir", "cloud_data/contrib")) / user["user_id"] / today
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{sha256[:12]}_{safe_name}"
    dest.write_bytes(raw)

    node_id = _ensure_contributor_node(user)
    cid = db.execute(
        "INSERT INTO contributions (user_id, node_id, filename, sha256, "
        " size_bytes, status, wcs_present, stored_path, date_obs, created_at) "
        "VALUES (%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s)",
        (user["user_id"], node_id, safe_name, sha256, len(raw),
         1 if probe["wcs"] else 0, str(dest), probe.get("date_obs", ""),
         _now()), returning_id=True)
    logger.info("Contribution #%s queued: %s (%d KB) from member %s",
                cid, safe_name, len(raw) // 1024, user["user_id"])
    return jsonify({"ok": True, "contribution_id": cid, "status": "pending",
                    "node_id": node_id})


@app.route("/api/v1/me/contributions/batch-manifest", methods=["POST"])
@auth.require_member
def api_me_contribute_manifest(user):
    """Archive dedup: a bulk uploader sends the sha256 list of an image folder
    and learns which frames are new, so years of history upload without
    re-sending anything the network already has (contributions.sha256 UNIQUE)."""
    body = request.get_json(force=True, silent=True) or {}
    shas = body.get("sha256") or []
    if not isinstance(shas, list) or not shas:
        return jsonify({"error": "sha256 list required"}), 400
    shas = [str(s).strip().lower() for s in shas[:5000] if str(s).strip()]
    if not shas:
        return jsonify({"error": "no valid sha256 values"}), 400
    known = {r["sha256"] for r in db.query(
        "SELECT sha256 FROM contributions WHERE sha256 = ANY(%s)", (shas,))}
    new = [s for s in shas if s not in known]
    return jsonify({"new": new, "known": sorted(known),
                    "n_new": len(new), "n_known": len(known)})


@app.route("/api/v1/me/discoveries", methods=["GET"])
@auth.require_member
def api_me_discoveries(user):
    """Discovery candidates this member's nodes (or contributions) touched —
    the feed behind 'your telescope may have found something new'."""
    node_ids = [r["node_id"] for r in db.query(
        "SELECT node_id FROM node_members WHERE user_id = %s",
        (user["user_id"],))]
    if not node_ids:
        return jsonify({"count": 0, "discoveries": []})
    rows = db.query(
        "SELECT id, source_key, ra_deg, dec_deg, kind, filter, first_bjd, "
        "       last_bjd, n_detections, n_nodes, node_ids, peak_delta_mag, "
        "       last_mag, state, vsx_name, tns_name, target_id, updated_at "
        "FROM discovery_candidates "
        "WHERE node_ids::text LIKE ANY(%s) "
        "ORDER BY updated_at DESC LIMIT 100",
        (["%" + nid + "%" for nid in node_ids],))
    for r in rows:
        touched = db.loads(r.pop("node_ids", "[]"), [])
        r["your_nodes"] = [n for n in touched if n in node_ids]
        r["retrospective"] = False
    # Retrospective discoveries this member's own uploads produced ("a nova was
    # in your 2023 frame") — credited by user_id on the contribution.
    retro = db.query(
        "SELECT id, source_key, ra_deg, dec_deg, kind, filter, bjd, mag, "
        "       delta_mag, state, vsx_name, tns_name, updated_at "
        "FROM retro_discoveries WHERE user_id = %s "
        "ORDER BY updated_at DESC LIMIT 100", (user["user_id"],))
    for r in retro:
        r["retrospective"] = True
    combined = rows + retro
    return jsonify({"count": len(combined), "discoveries": combined,
                    "live": len(rows), "retrospective": len(retro)})


@app.route("/api/v1/survey/lightcurves/<source_key>", methods=["GET"])
def api_survey_lightcurve(source_key: str):
    """Public light curve for any surveyed star — every measurement the
    network (scheduled nodes and contributors alike) has of it."""
    src = db.query_one(
        "SELECT source_key, filter, ra_deg, dec_deg, catalog_mag, catalog_src, "
        "       n_obs, mean_mag, m2, vsx_name, variability_flag, last_bjd "
        "FROM survey_sources WHERE source_key = %s "
        "ORDER BY n_obs DESC LIMIT 1", (source_key,))
    if src is None:
        return jsonify({"error": "unknown survey source"}), 404
    n = int(src.get("n_obs") or 0)
    src["stdev_mag"] = (round((float(src.pop("m2") or 0.0) / (n - 1)) ** 0.5, 4)
                        if n > 1 else None)
    points = db.query(
        "SELECT bjd, mag, mag_err, snr, filter, node_id "
        "FROM survey_measurements WHERE source_key = %s "
        "ORDER BY bjd DESC LIMIT 500", (source_key,))
    return jsonify({"source": src, "count": len(points),
                    "measurements": points})


@app.route("/api/v1/me/contributions", methods=["GET"])
@auth.require_member
def api_me_contributions(user):
    rows = db.query(
        "SELECT id, filename, status, n_sources, error, created_at, "
        "processed_at FROM contributions WHERE user_id = %s "
        "ORDER BY created_at DESC LIMIT 100", (user["user_id"],))
    return jsonify({"count": len(rows), "contributions": rows})


@app.route("/api/v1/admin/activation-codes", methods=["POST"])
@require_admin
def api_admin_generate_code():
    """Retired — activation codes no longer exist."""
    return jsonify({
        "error": "activation codes are retired — members use POST /me/nodes/attach",
    }), 410


@app.route("/api/v1/me/science-program-suggestions", methods=["POST"])
@auth.require_member
def api_me_science_program_suggestion(user):
    """Submit a science program idea from the member app."""
    body = request.get_json(force=True, silent=True) or {}
    title = str(body.get("title") or "").strip()
    description = str(body.get("description") or "").strip()
    if not title or not description:
        return jsonify({"error": "title and description are required"}), 400
    target_examples = str(body.get("target_examples") or "")[:2000]
    notes = str(body.get("notes") or "")[:2000]
    email = str(user.get("email") or "")[:200]
    created_at = _now()
    suggestion_id = db.execute(
        """INSERT INTO science_program_suggestions
               (user_id, email, title, description, target_examples, notes, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (user["user_id"], email, title[:200], description[:5000], target_examples, notes, created_at),
        returning_id=True,
    )
    return jsonify({"ok": True, "id": suggestion_id, "created_at": created_at})


@app.route("/api/v1/admin/science-program-suggestions", methods=["GET"])
@require_admin
def api_admin_science_program_suggestions():
    """List member-submitted science program ideas (X-Admin-Key required)."""
    rows = db.query(
        """SELECT id, user_id, email, title, description, target_examples, notes,
                  status, created_at
           FROM science_program_suggestions
           ORDER BY created_at DESC"""
    )
    return jsonify({"suggestions": rows, "total": len(rows)})


@app.route("/api/v1/admin/science-program-suggestions/<int:suggestion_id>/status", methods=["PATCH"])
@require_admin
def api_admin_science_program_suggestion_status(suggestion_id: int):
    body = request.get_json(force=True, silent=True) or {}
    status = str(body.get("status") or "").strip()
    if status not in ("pending", "reviewed", "accepted", "declined"):
        return jsonify({"error": "status must be pending, reviewed, accepted, or declined"}), 400
    db.execute(
        "UPDATE science_program_suggestions SET status = %s WHERE id = %s",
        (status, suggestion_id),
    )
    return jsonify({"ok": True})


@app.route("/api/v1/me/help", methods=["GET"])
@auth.require_member
def api_me_help_session(user):
    """Help tab: contact info, weekly quota, and recent chat history."""
    return jsonify(help_chat.get_session(user["user_id"]))


@app.route("/api/v1/me/help/chat", methods=["POST"])
@auth.require_member
def api_me_help_chat(user):
    """Send one help message to the OpenRouter assistant (5 user messages/week)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = help_chat.chat(
            user["user_id"],
            str(body.get("message") or ""),
            str(body.get("node_id") or "").strip() or None,
            _config,
        )
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 429
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(result)


@app.route("/api/v1/nodes/config-patches", methods=["GET"])
@require_node
def api_node_config_patches(node):
    """Pending config.yaml patches queued by the help assistant."""
    patches = help_chat.pending_patches(node["node_id"])
    return jsonify({"patches": patches})


@app.route("/api/v1/nodes/config-patches/<int:patch_id>/ack", methods=["POST"])
@require_node
def api_node_config_patch_ack(node, patch_id: int):
    body = request.get_json(force=True, silent=True) or {}
    ok = bool(body.get("ok", True))
    error = str(body.get("error") or "")[:500]
    help_chat.ack_patch(patch_id, node["node_id"], ok, error)
    return jsonify({"ok": True})


@app.route("/api/v1/me/notifications/prefs", methods=["PUT"])
@auth.require_member
def api_me_notification_prefs(user):
    body = request.get_json(force=True, silent=True) or {}
    fields, params = [], []
    for col in ("notification_email", "notification_push"):
        if col in body:
            fields.append(f"{col} = %s")
            params.append(1 if body[col] else 0)
    if "push_token" in body:
        fields.append("push_token = %s")
        params.append(str(body["push_token"])[:500])
    if not fields:
        return jsonify({"error": "no updatable fields"}), 400
    params.append(user["user_id"])
    db.execute(f"UPDATE members SET {', '.join(fields)} WHERE user_id = %s", tuple(params))
    return jsonify({"ok": True})


@app.route("/api/v1/me", methods=["DELETE"])
@auth.require_member
def api_me_delete(user):
    """
    Permanently delete the member's account and all associated data.
    Requires the member to confirm by sending {"confirm": true} in the body.
    """
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("confirm"):
        return jsonify({"error": "send {\"confirm\": true} to confirm deletion"}), 400

    uid = user["user_id"]
    # Remove all member-owned data in dependency order.
    db.execute("DELETE FROM sessions WHERE user_id = %s", (uid,))
    db.execute("DELETE FROM notifications WHERE user_id = %s", (uid,))
    db.execute("DELETE FROM help_chat_messages WHERE user_id = %s", (uid,))
    db.execute("DELETE FROM science_program_suggestions WHERE user_id = %s", (uid,))
    db.execute("DELETE FROM node_config_patches WHERE user_id = %s", (uid,))
    db.execute("DELETE FROM node_members WHERE user_id = %s", (uid,))
    db.execute("DELETE FROM activation_codes WHERE user_id = %s", (uid,))
    db.execute("DELETE FROM members WHERE user_id = %s", (uid,))
    db.execute("DELETE FROM users WHERE user_id = %s", (uid,))
    logger.info("Account deleted: %s (%s)", uid, user["email"])
    return jsonify({"ok": True})
