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
        "SELECT utc_offset_hours FROM nodes WHERE node_id = %s", (node_id,))
    offset_h = float((node or {}).get("utc_offset_hours", 0.0))

    # Night window in UTC: local 18:00 → next-day local 06:00
    night_dt = datetime.strptime(night, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_start = (night_dt + timedelta(hours=18 - offset_h)).isoformat()
    window_end   = (night_dt + timedelta(hours=30 - offset_h)).isoformat()

    rows = db.query(
        """SELECT target_name, bjd, magnitude, uncertainty,
                  quality_flag, validation_status, aavso_submitted
           FROM measurements
           WHERE node_id = %s AND received_at >= %s AND received_at < %s
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
    target_names = sorted(per_target.keys())
    if n_submitted:
        receipt_title = f"{n_submitted} measurement{'s' if n_submitted != 1 else ''} submitted to AAVSO"
    else:
        receipt_title = f"{n_obs} measurement{'s' if n_obs != 1 else ''} captured for review"
    if target_names:
        lead = (
            f"Your node observed {len(target_names)} target"
            f"{'s' if len(target_names) != 1 else ''}: {', '.join(target_names[:4])}"
            f"{' and more' if len(target_names) > 4 else ''}."
        )
    else:
        lead = "Your node checked in, but no usable measurements were captured."

    summary = {
        "node_id":       node_id,
        "night":         night,
        "n_targets":     len(per_target),
        "n_observations": n_obs,
        "n_submitted":   n_submitted,
        "targets":       per_target,
        "receipt": {
            "title": receipt_title,
            "lead": lead,
            "highlights": [
                f"{len(per_target)} target{'s' if len(per_target) != 1 else ''} covered",
                f"{n_obs} photometric measurement{'s' if n_obs != 1 else ''}",
                f"{n_submitted} AAVSO submission{'s' if n_submitted != 1 else ''}",
            ],
            "aavso_ready": n_submitted > 0,
        },
    }

    db.execute(
        """INSERT INTO night_summaries
               (node_id, night, n_targets, n_observations, n_submitted,
                summary_json, generated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
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


def _completed_night_for_node(now_utc: datetime, offset_h: float,
                              ready_local_hour: int = 8) -> str:
    """
    Latest local night that is safe to summarize.

    Plans are keyed by the local evening date.  A night that starts on Jul 3
    ends the morning of Jul 4, so before the local morning cutoff we must look
    back two calendar dates, not one UTC date.
    """
    local_now = now_utc + timedelta(hours=offset_h)
    days_back = 1 if local_now.hour >= ready_local_hour else 2
    return (local_now.date() - timedelta(days=days_back)).strftime("%Y-%m-%d")


def _store_missed_summary(node_id: str, night: str) -> None:
    summary = {
        "node_id":        node_id,
        "night":          night,
        "n_targets":      0,
        "n_observations": 0,
        "n_submitted":    0,
        "targets":        {},
        "receipt": {
            "title": "No measurements received",
            "lead": "A plan was scheduled, but no measurements arrived for this night.",
            "highlights": [
                "0 photometric measurements",
                "Node needs review before the next run",
            ],
            "aavso_ready": False,
        },
    }
    db.execute(
        """INSERT INTO night_summaries
               (node_id, night, n_targets, n_observations, n_submitted,
                summary_json, generated_at)
           VALUES (%s,%s,0,0,0,%s,%s)
           ON CONFLICT(node_id, night) DO UPDATE SET
               n_targets      = excluded.n_targets,
               n_observations = excluded.n_observations,
               n_submitted    = excluded.n_submitted,
               summary_json   = excluded.summary_json,
               generated_at   = excluded.generated_at""",
        (node_id, night, json.dumps(summary), _now()),
    )


def generate_pending_summaries(config: dict, now: datetime | None = None) -> int:
    """
    For every active node, generate last night's summary if it's missing.
    Called by the daily maintenance loop.  Returns the count of new summaries.
    """
    generated = 0
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    for node in db.query(
        "SELECT node_id, utc_offset_hours FROM nodes WHERE status = 'active'"
    ):
        nid = node["node_id"]
        night = _completed_night_for_node(
            now_utc, float(node.get("utc_offset_hours") or 0.0)
        )
        if db.query_one(
            "SELECT id FROM night_summaries WHERE node_id = %s AND night = %s",
            (nid, night),
        ):
            continue
        summary = generate_night_summary(nid, night)
        if summary:
            generated += 1
            _dispatch_notifications(nid, night, summary, config)
        else:
            if _dispatch_missed_night(nid, night, config):
                _store_missed_summary(nid, night)
                generated += 1
    return generated


def _dispatch_missed_night(node_id: str, night: str, config: dict | None = None) -> bool:
    """
    A node had a plan for `night` but produced zero measurements. Alert the
    member app so a stuck node (e.g. a plan sitting unrun, a dead ALPACA
    connection) is visible without anyone having to open the local dashboard.
    """
    plan = db.query_one(
        "SELECT plan_id FROM plans WHERE node_id = %s AND night = %s",
        (node_id, night),
    )
    if not plan:
        return False  # no plan was ever generated for this night — nothing to flag

    from cloud import push  # local import to avoid circular dependency

    members = db.query(
        """SELECT nm.user_id, m.push_token, m.notification_push
           FROM node_members nm
           JOIN members m ON m.user_id = nm.user_id
           WHERE nm.node_id = %s""",
        (node_id,),
    )
    payload = json.dumps({
        "node_id": node_id,
        "night": night,
        "title": "Telescope missed last night",
        "message": "A plan was scheduled, but no measurements came in.",
    })

    for m in members:
        db.execute(
            "INSERT INTO notifications (user_id, type, payload, sent_at) VALUES (%s,%s,%s,%s)",
            (m["user_id"], "node_missed_night", payload, _now()),
        )
        if config and m.get("notification_push") and m.get("push_token"):
            push.send(
                fcm_token=m["push_token"],
                title="Your telescope didn't observe last night",
                body="A plan was scheduled but no measurements came in. Check the node — "
                     "it may need a restart or a setting re-enabled.",
                data={"type": "node_missed_night", "node_id": node_id, "night": night},
                config=config,
            )

    if members:
        logger.warning(
            "Node %s had a plan on %s but zero measurements — dispatched "
            "node_missed_night to %d member(s)", node_id, night, len(members),
        )
    return True


def _dispatch_notifications(
    node_id: str, night: str, summary: dict, config: dict | None = None
) -> None:
    """
    Write a notification record for every member who owns this node, and fire
    an FCM push if the member has a registered device token.
    """
    from cloud import push  # local import to avoid circular dependency

    members = db.query(
        """SELECT nm.user_id, m.push_token, m.notification_push
           FROM node_members nm
           JOIN members m ON m.user_id = nm.user_id
           WHERE nm.node_id = %s""",
        (node_id,),
    )
    payload = json.dumps({"node_id": node_id, "night": night, "summary": summary})

    n_targets = summary.get("n_targets", 0)
    n_submitted = summary.get("n_submitted", 0)

    for m in members:
        db.execute(
            "INSERT INTO notifications (user_id, type, payload, sent_at) VALUES (%s,%s,%s,%s)",
            (m["user_id"], "night_summary", payload, _now()),
        )

        # Fire FCM push if the member opted in and has a device token.
        if config and m.get("notification_push") and m.get("push_token"):
            target_word = "target" if n_targets == 1 else "targets"
            push.send(
                fcm_token=m["push_token"],
                title=f"Night summary · {night}",
                body=(
                    f"{n_targets} {target_word} observed, "
                    f"{n_submitted} submitted to AAVSO."
                ),
                data={
                    "type": "night_summary",
                    "node_id": node_id,
                    "night": night,
                },
                config=config,
            )

    if members:
        logger.info(
            "Dispatched night_summary notifications: %d member(s) for %s %s",
            len(members), node_id, night,
        )
