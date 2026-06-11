#!/usr/bin/env python3
"""
Night summary generation and notification dispatch.

After each observing night, summarises what each node observed, which
measurements passed AAVSO submission, and stores the result for the member
app.  Notification records are written to the notifications table so the
app can show the morning digest.

Public API
----------
    generate_night_summary(node_id, night)  → summary dict | None
    generate_pending_summaries(config)      → int  (count generated)
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from cloud import db

logger = logging.getLogger("cloud.nights")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_night_summary(node_id: str, night: str) -> dict | None:
    """
    Build and store a summary for `node_id` on `night` (YYYY-MM-DD, local
    evening date).  Uses the node's utc_offset_hours to define the observation
    window as 18:00–06:00 local time.

    Returns the summary dict, or None if no measurements exist for that night.
    """
    node = db.query_one(
        "SELECT utc_offset_hours FROM nodes WHERE node_id = ?", (node_id,))
    offset_h = float((node or {}).get("utc_offset_hours", 0.0))

    # Night window in UTC: local 18:00 → next-day local 06:00
    night_dt = datetime.strptime(night, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_start = (night_dt + timedelta(hours=18 - offset_h)).isoformat()
    window_end   = (night_dt + timedelta(hours=30 - offset_h)).isoformat()

    rows = db.query(
        """SELECT target_name, bjd, magnitude, uncertainty,
                  quality_flag, validation_status, aavso_submitted
           FROM measurements
           WHERE node_id = ? AND received_at >= ? AND received_at < ?
           ORDER BY bjd""",
        (node_id, window_start, window_end),
    )
    if not rows:
        return None

    # Aggregate per target
    targets: dict[str, dict] = {}
    for r in rows:
        tn = r["target_name"]
        if tn not in targets:
            targets[tn] = {"n_obs": 0, "n_submitted": 0, "quality_counts": {}}
        targets[tn]["n_obs"] += 1
        if r["aavso_submitted"]:
            targets[tn]["n_submitted"] += 1
        qf = r["quality_flag"]
        targets[tn]["quality_counts"][qf] = targets[tn]["quality_counts"].get(qf, 0) + 1

    # Simplify quality to a single label per target
    per_target = {}
    for tn, t in targets.items():
        counts = t["quality_counts"]
        if counts.get("good", 0) > 0:
            quality = "good"
        elif counts.get("acceptable", 0) > 0:
            quality = "acceptable"
        else:
            quality = "poor"
        per_target[tn] = {
            "n_observations": t["n_obs"],
            "n_submitted":    t["n_submitted"],
            "quality":        quality,
        }

    n_obs = sum(t["n_observations"] for t in per_target.values())
    n_submitted = sum(t["n_submitted"] for t in per_target.values())

    summary = {
        "node_id":       node_id,
        "night":         night,
        "n_targets":     len(per_target),
        "n_observations": n_obs,
        "n_submitted":   n_submitted,
        "targets":       per_target,
    }

    db.execute(
        """INSERT INTO night_summaries
               (node_id, night, n_targets, n_observations, n_submitted,
                summary_json, generated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(node_id, night) DO UPDATE SET
               n_targets      = excluded.n_targets,
               n_observations = excluded.n_observations,
               n_submitted    = excluded.n_submitted,
               summary_json   = excluded.summary_json,
               generated_at   = excluded.generated_at""",
        (node_id, night, len(per_target), n_obs, n_submitted,
         json.dumps(summary), _now()),
    )
    logger.info(
        "Night summary %s %s: %d obs on %d targets, %d submitted",
        node_id, night, n_obs, len(per_target), n_submitted,
    )
    return summary


def generate_pending_summaries(config: dict) -> int:
    """
    For every active node, generate last night's summary if it's missing.
    Called by the daily maintenance loop.  Returns the count of new summaries.
    """
    generated = 0
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    for node in db.query("SELECT node_id FROM nodes WHERE status = 'active'"):
        nid = node["node_id"]
        if db.query_one(
            "SELECT id FROM night_summaries WHERE node_id = ? AND night = ?",
            (nid, yesterday),
        ):
            continue
        summary = generate_night_summary(nid, yesterday)
        if summary:
            generated += 1
            _dispatch_notifications(nid, yesterday, summary)
    return generated


def _dispatch_notifications(node_id: str, night: str, summary: dict) -> None:
    """Write a notification record for every member who owns this node."""
    members = db.query(
        "SELECT user_id FROM node_members WHERE node_id = ?", (node_id,))
    payload = json.dumps({"node_id": node_id, "night": night, "summary": summary})
    for m in members:
        db.execute(
            "INSERT INTO notifications (user_id, type, payload, sent_at) VALUES (?,?,?,?)",
            (m["user_id"], "night_summary", payload, _now()),
        )
    if members:
        logger.info(
            "Dispatched night_summary notifications: %d member(s) for %s %s",
            len(members), node_id, night,
        )
