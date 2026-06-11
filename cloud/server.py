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
    GET  /api/v1/interrupts              → unexpired interrupts for this node

Public/query endpoints (for dashboard & app):
    GET  /api/v1/targets                 → active targets with best scores
    GET  /api/v1/lightcurves/<name>      → aggregated light curve
    GET  /api/v1/network/status          → node + data summary

Admin endpoints (X-Admin-Key header):
    POST /api/v1/interrupts              → broadcast a high-priority target
    POST /api/v1/admin/ingest            → run alert ingestion now
    POST /api/v1/admin/replan            → rescore + regenerate all plans
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, jsonify, request

from cloud import alerts, data_pipeline, db, registry, scheduler, scoring

logger = logging.getLogger("cloud.server")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024

_config: dict = {}   # set by create_app()


def create_app(config: dict) -> Flask:
    global _config
    _config = config
    return app


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

@app.route("/api/v1/nodes/register", methods=["POST"])
def api_register():
    info = request.get_json(force=True, silent=True) or {}
    try:
        creds = registry.register_node(
            info, _config.get("light_pollution", {}).get("api_key", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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


# ── Interrupts ─────────────────────────────────────────────────────────────────

@app.route("/api/v1/interrupts", methods=["GET"])
@require_node
def api_interrupts_get(node):
    rows = db.query(
        "SELECT * FROM interrupts WHERE expires_at > ?", (_now(),))
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
    row = db.query_one("SELECT acked_by FROM interrupts WHERE id = ?", (interrupt_id,))
    if row is None:
        return jsonify({"error": "unknown interrupt"}), 404
    acked = db.loads(row["acked_by"], [])
    if node["node_id"] not in acked:
        acked.append(node["node_id"])
        db.execute("UPDATE interrupts SET acked_by = ? WHERE id = ?",
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
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (body.get("target_id"), name, ra_deg, dec_deg, body.get("mag"),
         str(body.get("reason", "")),
         json.dumps(body["node_ids"]) if body.get("node_ids") else None,
         _now(),
         (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()),
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
        "SELECT COUNT(*) AS n FROM measurements WHERE received_at > ?",
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


@app.route("/api/v1/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "server_time": _now()})
