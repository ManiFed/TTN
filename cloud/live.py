#!/usr/bin/env python3
"""
Live fleet state and the server→node dispatch bus.

Two responsibilities:

  1. record_state(node_id, state) — fold a node's second-scale phase report
     (from its heartbeat) into node_live_state. One row per node.

  2. publish(node_id, kind, payload) — append a dispatch event and fire a
     Postgres NOTIFY so the realtime SSE service (realtime/app.py) can push it
     to the node within milliseconds instead of waiting for the next poll.

The dispatch payload is deliberately a *signal*, not content: a node that
receives {kind: "interrupt"} re-fetches over the authenticated main API. The
stream is an accelerator; the 300 s plan poll and interrupt-on-heartbeat poll
remain the correctness path, so a node that never connects to SSE still works.

fleet_state() is the read side powering GET /api/v1/network/fleet and the
member "live fleet" view. 'offline' is computed here from updated_at age (3×
the node's own heartbeat cadence) — no background reaper writes it.
"""

import json
import logging
from datetime import datetime, timezone

from cloud import db

logger = logging.getLogger("cloud.live")

NOTIFY_CHANNEL = "dispatch"

# Dispatch events older than this are pruned by the maintenance loop; they only
# exist to let a reconnecting SSE client replay what it missed.
DISPATCH_RETENTION_HOURS = 6

# A node is 'offline' once it misses this many heartbeat intervals.
_STALE_MULTIPLIER = 3.0
# Fallback used when a node has never reported its heartbeat cadence.
_DEFAULT_HEARTBEAT_S = 60.0

_VALID_PHASES = {
    "idle", "slewing", "exposing", "stacking",
    "clouded", "offline", "daylight", "parked",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_state(node_id: str, state: dict, heartbeat_s: float = _DEFAULT_HEARTBEAT_S) -> None:
    """Upsert a node's live phase. Called from the heartbeat handler.

    Unknown/garbage phases collapse to 'idle' so a misbehaving node can't inject
    arbitrary strings into the fleet view. All fields are optional.
    """
    if not isinstance(state, dict):
        return
    phase = str(state.get("phase") or "idle").lower()
    if phase not in _VALID_PHASES:
        phase = "idle"

    def _num(key):
        val = state.get(key)
        return float(val) if isinstance(val, (int, float)) else None

    plan_idx = state.get("plan_item_idx")
    plan_idx = int(plan_idx) if isinstance(plan_idx, int) else None

    detail = state.get("detail")
    detail_json = json.dumps(detail) if isinstance(detail, dict) else "{}"
    if len(detail_json) > 2000:
        detail_json = "{}"

    db.execute(
        """INSERT INTO node_live_state
               (node_id, phase, target_name, plan_item_idx, exposure_ends_at,
                sky_clear, is_dark, heartbeat_s, updated_at, detail)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (node_id) DO UPDATE SET
               phase = EXCLUDED.phase,
               target_name = EXCLUDED.target_name,
               plan_item_idx = EXCLUDED.plan_item_idx,
               exposure_ends_at = EXCLUDED.exposure_ends_at,
               sky_clear = EXCLUDED.sky_clear,
               is_dark = EXCLUDED.is_dark,
               heartbeat_s = EXCLUDED.heartbeat_s,
               updated_at = EXCLUDED.updated_at,
               detail = EXCLUDED.detail""",
        (node_id, phase, str(state.get("target_name") or ""), plan_idx,
         str(state.get("exposure_ends_at") or ""), _num("sky_clear"),
         1 if state.get("is_dark") else 0, float(heartbeat_s or _DEFAULT_HEARTBEAT_S),
         _now(), detail_json),
    )


def publish(node_id: str, kind: str, payload: dict | None = None) -> int:
    """Append a dispatch event and NOTIFY listeners. Returns the event id.

    Best-effort: a NOTIFY failure never breaks the caller — the row is still
    written, so a reconnecting SSE client replays it, and the node's own polling
    would pick the work up regardless.
    """
    payload_json = json.dumps(payload or {})
    if len(payload_json) > 4000:
        payload_json = "{}"
    eid = db.execute(
        """INSERT INTO dispatch_events (node_id, kind, payload, created_at)
           VALUES (%s,%s,%s,%s)""",
        (node_id, kind, payload_json, _now()),
        returning_id=True,
    )
    try:
        # NOTIFY payload is size-limited (~8 KB); send only the routing info the
        # SSE service needs to decide which connection(s) to wake and where to
        # resume the replay from.
        note = json.dumps({"id": eid, "node_id": node_id, "kind": kind})
        conn = db.connect()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(f"NOTIFY {NOTIFY_CHANNEL}, %s", (note,))
        finally:
            db.release(conn)
    except Exception as exc:  # pragma: no cover - NOTIFY best-effort
        logger.debug("NOTIFY failed for event %s: %s", eid, exc)
    return eid


def events_since(node_id: str, last_id: int, limit: int = 100) -> list[dict]:
    """Replay dispatch events a reconnecting SSE client missed."""
    rows = db.query(
        """SELECT id, kind, payload, created_at FROM dispatch_events
           WHERE node_id = %s AND id > %s ORDER BY id LIMIT %s""",
        (node_id, last_id, limit),
    )
    for r in rows:
        r["payload"] = db.loads(r.get("payload"), {})
    return rows


def _is_stale(updated_at: str, heartbeat_s: float) -> bool:
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(updated_at)).total_seconds()
    except (TypeError, ValueError):
        return True
    return age > _STALE_MULTIPLIER * max(heartbeat_s or _DEFAULT_HEARTBEAT_S, 5.0)


def fleet_state() -> list[dict]:
    """Live phase of every node with a live-state row. Derives 'offline'."""
    rows = db.query(
        """SELECT node_id, phase, target_name, plan_item_idx, exposure_ends_at,
                  sky_clear, is_dark, heartbeat_s, updated_at, detail
           FROM node_live_state""",
    )
    out = []
    for r in rows:
        stale = _is_stale(r["updated_at"], r.get("heartbeat_s") or _DEFAULT_HEARTBEAT_S)
        out.append({
            "node_id":       r["node_id"],
            "phase":         "offline" if stale else r["phase"],
            "target_name":   r.get("target_name") or "",
            "plan_item_idx": r.get("plan_item_idx"),
            "sky_clear":     r.get("sky_clear"),
            "is_dark":       bool(r.get("is_dark")),
            "exposure_ends_at": r.get("exposure_ends_at") or "",
            "updated_at":    r["updated_at"],
            "online":        not stale,
        })
    return out


def node_live(node_id: str) -> dict | None:
    """Live state for a single node, or None if it has never reported."""
    r = db.query_one(
        "SELECT * FROM node_live_state WHERE node_id = %s", (node_id,))
    if r is None:
        return None
    stale = _is_stale(r["updated_at"], r.get("heartbeat_s") or _DEFAULT_HEARTBEAT_S)
    return {
        "node_id":          r["node_id"],
        "phase":            "offline" if stale else r["phase"],
        "target_name":      r.get("target_name") or "",
        "plan_item_idx":    r.get("plan_item_idx"),
        "sky_clear":        r.get("sky_clear"),
        "is_dark":          bool(r.get("is_dark")),
        "exposure_ends_at": r.get("exposure_ends_at") or "",
        "updated_at":       r["updated_at"],
        "online":           not stale,
        "detail":           db.loads(r.get("detail"), {}),
    }


def dark_online_nodes() -> set[str]:
    """Node ids currently dark, clear and online — the pool reflow/reflex draw
    from. Used by Phase 2; lives here so live-state semantics stay in one place.
    """
    out = set()
    for n in fleet_state():
        if n["online"] and n["is_dark"] and n["phase"] not in ("clouded", "daylight"):
            out.add(n["node_id"])
    return out


def prune_dispatch_events() -> int:
    """Delete dispatch events past the replay retention window."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=DISPATCH_RETENTION_HOURS)).isoformat()
    conn = db.connect()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM dispatch_events WHERE created_at < %s", (cutoff,))
            return cur.rowcount or 0
    finally:
        db.release(conn)
