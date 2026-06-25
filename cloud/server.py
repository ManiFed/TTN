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
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory

from cloud import alerts, auth, data_pipeline, db, nights, registry, scheduler, scoring, tuning

logger = logging.getLogger("cloud.server")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024

_config: dict = {}   # set by create_app()

_WEBSITE_DIR = os.path.join(os.path.dirname(__file__), "..", "website")
_DASHBOARD_DIR = os.path.join(_WEBSITE_DIR, "dashboard")
_APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "build", "web")


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
    full = os.path.join(_DASHBOARD_DIR, filename)
    if os.path.isfile(full):
        return send_from_directory(_DASHBOARD_DIR, filename)
    return send_from_directory(_DASHBOARD_DIR, "index.html")


@app.route("/app")
@app.route("/app/")
def serve_app():
    return send_from_directory(_APP_DIR, "index.html")


@app.route("/app/<path:filename>")
def serve_app_asset(filename):
    full = os.path.join(_APP_DIR, filename)
    if os.path.isfile(full):
        return send_from_directory(_APP_DIR, filename)
    return send_from_directory(_APP_DIR, "index.html")


@app.route("/<path:filename>")
def serve_website(filename):
    full = os.path.join(_WEBSITE_DIR, filename)
    if os.path.isfile(full):
        return send_from_directory(_WEBSITE_DIR, filename)
    return send_from_directory(_WEBSITE_DIR, "tour.html")


# ── Software downloads ─────────────────────────────────────────────────────────
# GitHub Releases are the canonical source. The endpoint redirects so the URL
# on the website stays stable even as release tags change.

from flask import redirect as _redirect

_GITHUB_RELEASE_BASE = "https://github.com/skyrobot804/node_v1/releases/download"

_DOWNLOAD_URLS = {
    "macos":   f"{_GITHUB_RELEASE_BASE}/v1.0.0/BoundlessSkiesNode-1.0.0-macOS.pkg",
    "windows": None,  # not yet released
    "linux":   None,  # not yet released
}

@app.route("/download/node-agent")
@app.route("/download/node-agent/<platform>")
def download_node_agent(platform: str = "macos"):
    url = _DOWNLOAD_URLS.get(platform)
    if url is None:
        return jsonify({"error": f"'{platform}' installer not yet available"}), 404
    return _redirect(url, code=302)


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


@app.route("/api/v1/nodes/register", methods=["POST"])
def api_register():
    info = request.get_json(force=True, silent=True) or {}
    activation_code = str(info.pop("activation_code", "") or "").strip().upper()

    try:
        creds = registry.register_node(
            info, _config.get("light_pollution", {}).get("api_key", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Consume the activation code and link the node to the member account
    if activation_code:
        try:
            user_id = _validate_and_consume_code(activation_code, creds["node_id"])
        except ValueError as exc:
            logger.warning("Activation code error for %s: %s", creds["node_id"], exc)
            # Registration still succeeds — the node works, just isn't linked
            creds["activation_warning"] = str(exc)
        else:
            if user_id:
                if not db.query_one(
                    "SELECT 1 FROM node_members WHERE node_id = %s AND user_id = %s",
                    (creds["node_id"], user_id),
                ):
                    db.execute(
                        "INSERT INTO node_members (node_id, user_id, claimed_at)"
                        " VALUES (%s,%s,%s)",
                        (creds["node_id"], user_id, _now()),
                    )
            logger.info("Activation code %s consumed — node %s linked to user %s",
                        activation_code, creds["node_id"], user_id or "(generic)")

    return jsonify(creds)


@app.route("/api/v1/nodes/heartbeat", methods=["POST"])
@require_node
def api_heartbeat(node):
    body = request.get_json(force=True, silent=True) or {}
    registry.heartbeat(node["node_id"], body.get("conditions"))
    return jsonify({"ok": True, "server_time": _now()})


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


# ── Measurements & images ──────────────────────────────────────────────────────

@app.route("/api/v1/measurements", methods=["POST"])
@require_node
def api_measurements(node):
    body = request.get_json(force=True, silent=True) or {}
    measurement = body.get("measurement") or body   # accept bare measurement dicts
    result = data_pipeline.ingest_measurement(
        node["node_id"], measurement, body.get("conditions"))
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
    from pathlib import Path
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
    from pathlib import Path
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
    import re as _re
    # Guard against path traversal
    if ".." in rel or rel.startswith("/"):
        return jsonify({"error": "invalid path"}), 400
    root = _aavso_file_dir()
    abs_path = (root / rel).resolve()
    if not str(abs_path).startswith(str(root.resolve())):
        return jsonify({"error": "invalid path"}), 400
    if not abs_path.exists():
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
    iid = db.execute(
        """INSERT INTO interrupts
               (target_id, name, ra_deg, dec_deg, mag, reason, node_ids,
                created_at, expires_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (body.get("target_id"), name, ra_deg, dec_deg, body.get("mag"),
         str(body.get("reason", "")),
         json.dumps(body["node_ids"]) if body.get("node_ids") else None,
         _now(),
         (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()),
        returning_id=True,
    )
    logger.info("Interrupt #%d created: %s (%.4f, %.4f)", iid, name, ra_deg, dec_deg)
    return jsonify({"ok": True, "id": iid})


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
    return jsonify({"targets": rows})


@app.route("/api/v1/lightcurves/<path:target_name>", methods=["GET"])
def api_lightcurve(target_name: str):
    days = float(request.args.get("days", 365))
    points = data_pipeline.light_curve(target_name, days)
    return jsonify({"target": target_name, "n": len(points), "points": points})


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


@app.route("/api/v1/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "server_time": _now()})


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


@app.route("/api/v1/auth/login", methods=["POST"])
def api_auth_login():
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = auth.login(body.get("email", ""), body.get("password", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 401
    return jsonify(result)


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
        """SELECT n.node_id, n.telescope_model, n.city, n.country, n.status,
                  n.last_heartbeat, nm.claimed_at
           FROM nodes n
           JOIN node_members nm ON nm.node_id = n.node_id
           WHERE nm.user_id = %s""",
        (user["user_id"],),
    )
    for r in rows:
        r["online"] = registry.is_online(r)
    return jsonify({"nodes": rows})


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


@app.route("/api/v1/me/observations", methods=["GET"])
@auth.require_member
def api_me_observations(user):
    """Observations from all nodes owned by this member."""
    days = min(int(request.args.get("days", 90)), 365)
    limit = min(int(request.args.get("limit", 200)), 1000)

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
    return jsonify({
        "total_observations": totals.get("total", 0) or 0,
        "aavso_submitted":    int(totals.get("submitted", 0) or 0),
        "targets_observed":   totals.get("targets", 0) or 0,
        "clear_nights":       int(clear.get("clear_nights", 0) or 0),
        "node_count":         len(node_ids),
    })


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
        r["targets"] = db.loads(r.pop("summary_json"), {}).get("targets", {})
    return jsonify({"nights": rows})


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
    """
    Generate a personal activation code for the logged-in member.
    Used during the installer flow to link a new node to the account.
    """
    code = _generate_activation_code()
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    db.execute(
        "INSERT INTO activation_codes (code, user_id, created_at, expires_at)"
        " VALUES (%s,%s,%s,%s)",
        (code, user["user_id"], _now(), expires),
    )
    logger.info("Activation code generated for member %s: %s", user["user_id"], code)
    return jsonify({"code": code, "expires_at": expires})


@app.route("/api/v1/admin/activation-codes", methods=["POST"])
@require_admin
def api_admin_generate_code():
    """Generate activation codes in bulk (admin). Optional user_id links them."""
    body = request.get_json(force=True, silent=True) or {}
    n = min(int(body.get("count", 1)), 100)
    user_id = body.get("user_id")
    days = int(body.get("expires_days", 90))
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    codes = []
    for _ in range(n):
        code = _generate_activation_code()
        db.execute(
            "INSERT INTO activation_codes (code, user_id, created_at, expires_at)"
            " VALUES (%s,%s,%s,%s)",
            (code, user_id, _now(), expires),
        )
        codes.append(code)
    return jsonify({"codes": codes, "expires_at": expires})


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
