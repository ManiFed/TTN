#!/usr/bin/env python3
"""Cloud-side creation and verification primitives for offline night bundles."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from cloud import db, scheduler
from src.shared_models import AutonomyBundle


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def canonical_payload(bundle: dict) -> bytes:
    body = dict(bundle)
    body.pop("signature", None)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _private_key(config: dict):
    raw = str((config.get("autonomy") or {}).get("private_key")
              or os.environ.get("AUTONOMY_SIGNING_KEY", "")).strip()
    if not raw:
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        key_bytes = base64.b64decode(raw)
    except Exception as exc:
        raise ValueError("AUTONOMY_SIGNING_KEY must be base64") from exc
    if len(key_bytes) != 32:
        raise ValueError("AUTONOMY_SIGNING_KEY must contain 32 raw Ed25519 bytes")
    return Ed25519PrivateKey.from_private_bytes(key_bytes)


def public_key_b64(config: dict) -> str:
    key = _private_key(config)
    if key is None:
        return ""
    from cryptography.hazmat.primitives import serialization
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign_bundle(bundle: dict, config: dict) -> dict:
    key = _private_key(config)
    if key is None:
        raise RuntimeError("autonomy signing key is not configured")
    out = dict(bundle)
    out["signature"] = base64.b64encode(key.sign(canonical_payload(out))).decode()
    return out


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _item_window(items: list, now: datetime) -> tuple[datetime, datetime]:
    starts = []
    ends = []
    for item in items:
        value = item.get("starts_at_utc")
        if not value:
            continue
        try:
            start = _parse(value)
        except ValueError:
            continue
        starts.append(start)
        duration = (float(item.get("duration_minutes") or 0.0)
                    if item.get("observation_mode") == "time_series"
                    else float(item.get("expDur") or 0.0)
                         * int(item.get("expCount") or 0) / 60.0)
        ends.append(start + timedelta(minutes=max(duration, 15.0)))
    valid_from = max(now, min([s - timedelta(hours=4) for s in starts], default=now))
    desired_end = max([now + timedelta(hours=1)] + [e + timedelta(hours=1) for e in ends])
    return valid_from, min(desired_end, valid_from + timedelta(hours=18))


def build_for_node(node_id: str, config: dict, force: bool = False) -> Optional[dict]:
    """Return the current signed bundle, creating it from the current plan."""
    acfg = config.get("autonomy") or {}
    if not acfg.get("enabled", False):
        return None
    existing = db.query_one(
        "SELECT payload FROM autonomy_bundles WHERE node_id=%s "
        "AND status IN ('current','reconciled') "
        "ORDER BY sequence DESC LIMIT 1", (node_id,))
    plan = scheduler.current_plan(node_id)
    if not plan:
        return None
    if existing and not force:
        payload = db.loads(existing["payload"], {})
        try:
            still_valid = _parse(str(payload.get("expires_at") or "")) > _now_dt()
        except ValueError:
            still_valid = False
        if payload.get("plan_id") == plan.get("plan_id") and still_valid:
            return payload
    item_budget = min(100, max(1, int(acfg.get("max_items", 50))),
                      max(1, int(acfg.get("max_slews", 50))))
    items = [dict(i) for i in (plan.get("items") or [])[:item_budget]]
    for idx, item in enumerate(items):
        if not item.get("item_id"):
            stable = f"{plan['plan_id']}:{idx}:{item.get('target','')}"
            item["item_id"] = "item_" + hashlib.sha256(stable.encode()).hexdigest()[:16]
        item.setdefault("task_type", "science")
    contingency_budget = max(0, item_budget - len(items))
    contingencies = [dict(i) for i in
                     ((plan.get("contingencies") or {}).get("alternates") or [])
                     [:contingency_budget]]
    primary_starts = [i.get("starts_at_utc") for i in items if i.get("starts_at_utc")]
    primary_latest = [i.get("latest_start_utc") for i in items if i.get("latest_start_utc")]
    for idx, item in enumerate(contingencies):
        stable = f"{plan['plan_id']}:contingency:{idx}:{item.get('target','')}"
        item["item_id"] = "item_" + hashlib.sha256(stable.encode()).hexdigest()[:16]
        item["starts_at_utc"] = min(primary_starts) if primary_starts else _iso(_now_dt())
        item["latest_start_utc"] = max(primary_latest) if primary_latest else _iso(
            _now_dt() + timedelta(hours=12))
        item["startTime"] = item.get("earliestStart", "")
        item["task_type"] = "science"
        item["priority"] = float(item.get("expected_info") or 0)
    now = _now_dt()
    valid_from, expires = _item_window(items, now)
    previous = db.query_one(
        "SELECT MAX(sequence) AS n FROM autonomy_bundles WHERE node_id=%s", (node_id,)) or {}
    sequence = int(previous.get("n") or 0) + 1
    key_id = str(acfg.get("signing_key_id") or "primary")
    bundle = AutonomyBundle(
        bundle_id="bundle_" + uuid.uuid4().hex[:16], sequence=sequence,
        node_id=node_id, plan_id=plan["plan_id"], issued_at=_iso(now),
        valid_from=_iso(valid_from), expires_at=_iso(expires), items=items,
        contingencies={"alternates": contingencies},
        budgets={
            "max_items": item_budget,
            "max_exposure_s": float(acfg.get("max_exposure_s", 12 * 3600)),
            "max_slews": int(acfg.get("max_slews", 50)),
            "max_storage_bytes": int(acfg.get("max_storage_bytes", 20 * 1024**3)),
        },
        requirements={"clock_skew_s": 30, "commissioning_complete": True},
        config_fingerprint=str(acfg.get("config_fingerprint") or ""),
        signing_key_id=key_id,
    ).to_dict()
    if acfg.get("next_signing_key_id") and acfg.get("next_public_key"):
        bundle["next_public_key"] = {
            "key_id": str(acfg["next_signing_key_id"]),
            "public_key": str(acfg["next_public_key"]),
        }
    bundle = sign_bundle(bundle, config)
    db.execute("UPDATE autonomy_bundles SET status='superseded' WHERE node_id=%s AND status='current'",
               (node_id,))
    db.execute(
        "INSERT INTO autonomy_bundles "
        "(bundle_id,node_id,plan_id,sequence,issued_at,valid_from,expires_at,payload,"
        " signature,signing_key_id,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'current')",
        (bundle["bundle_id"], node_id, plan["plan_id"], sequence,
         bundle["issued_at"], bundle["valid_from"], bundle["expires_at"],
         json.dumps(bundle), bundle["signature"], key_id))
    return bundle


def store_outcomes(node_id: str, outcomes: list) -> dict:
    accepted = duplicates = rejected = 0
    touched_bundles = set()
    allowed = {"received", "started", "completed", "skipped", "failed", "cancelled"}
    for raw in outcomes[:1000]:
        attempt_id = str(raw.get("attempt_id") or "")
        state = str(raw.get("state") or "")
        if not attempt_id or state not in allowed:
            rejected += 1
            continue
        if raw.get("bundle_id"):
            touched_bundles.add(str(raw["bundle_id"]))
        before = db.query_one("SELECT 1 FROM execution_outcomes WHERE attempt_id=%s", (attempt_id,))
        if before:
            existing = db.query_one("SELECT state FROM execution_outcomes WHERE attempt_id=%s",
                                    (attempt_id,)) or {}
            rank = {"received": 0, "started": 1, "completed": 2, "skipped": 2,
                    "failed": 2, "cancelled": 2}
            if rank.get(state, 0) > rank.get(existing.get("state"), 0):
                db.execute(
                    "UPDATE execution_outcomes SET state=%s,finished_at=%s,frames_attempted=%s,"
                    "frames_completed=%s,last_checkpoint=%s,failure_reason=%s,detail=%s,received_at=%s "
                    "WHERE attempt_id=%s",
                    (state, str(raw.get("finished_at") or ""),
                     int(raw.get("frames_attempted") or 0),
                     int(raw.get("frames_completed") or 0),
                     str(raw.get("last_checkpoint") or ""),
                     str(raw.get("failure_reason") or "")[:500],
                     json.dumps(raw.get("detail") or {}), _iso(_now_dt()), attempt_id))
                task_id = str(raw.get("task_id") or "")
                if task_id:
                    db.execute("UPDATE observation_tasks SET state=%s,updated_at=%s "
                               "WHERE task_id=%s AND node_id=%s",
                               (state, _iso(_now_dt()), task_id, node_id))
                accepted += 1
            else:
                duplicates += 1
            continue
        db.execute(
            "INSERT INTO execution_outcomes "
            "(attempt_id,node_id,item_id,bundle_id,task_id,state,started_at,finished_at,"
            " frames_attempted,frames_completed,last_checkpoint,failure_reason,detail,received_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (attempt_id, node_id, str(raw.get("item_id") or ""),
             str(raw.get("bundle_id") or ""), str(raw.get("task_id") or ""),
             state, str(raw.get("started_at") or ""), str(raw.get("finished_at") or ""),
             int(raw.get("frames_attempted") or 0), int(raw.get("frames_completed") or 0),
             str(raw.get("last_checkpoint") or ""),
             str(raw.get("failure_reason") or "")[:500],
             json.dumps(raw.get("detail") or {}), _iso(_now_dt())))
        task_id = str(raw.get("task_id") or "")
        if task_id:
            db.execute("UPDATE observation_tasks SET state=%s,updated_at=%s WHERE task_id=%s AND node_id=%s",
                       (state, _iso(_now_dt()), task_id, node_id))
        accepted += 1
    reconciled = []
    terminal = {"completed", "skipped", "failed", "cancelled"}
    for bundle_id in touched_bundles:
        bundle_row = db.query_one(
            "SELECT payload,status FROM autonomy_bundles WHERE bundle_id=%s AND node_id=%s",
            (bundle_id, node_id))
        if not bundle_row:
            continue
        payload = db.loads(bundle_row.get("payload"), {})
        expected = {str(i.get("item_id") or "") for i in payload.get("items") or []
                    if i.get("item_id")}
        rows = db.query(
            "SELECT item_id,state,started_at,finished_at,detail FROM execution_outcomes "
            "WHERE bundle_id=%s AND node_id=%s", (bundle_id, node_id))
        finished = {str(r.get("item_id") or "") for r in rows
                    if r.get("state") in terminal}
        offline = [r for r in rows if db.loads(r.get("detail"), {}).get("offline")]
        starts, ends = [], []
        for row in offline:
            try:
                if row.get("started_at"):
                    starts.append(_parse(str(row["started_at"])))
                if row.get("finished_at"):
                    ends.append(_parse(str(row["finished_at"])))
            except ValueError:
                pass
        reconciliation = {
            "expected_items": len(expected), "terminal_items": len(expected & finished),
            "offline_attempts": len(offline),
            "offline_duration_s": (max(0.0, (max(ends) - min(starts)).total_seconds())
                                   if starts and ends else 0.0),
            "updated_at": _iso(_now_dt()),
        }
        is_complete = bool(expected) and expected <= finished
        db.execute("UPDATE autonomy_bundles SET reconciliation=%s,status=%s WHERE bundle_id=%s",
                   (json.dumps(reconciliation),
                    "reconciled" if is_complete else str(bundle_row.get("status") or "current"),
                    bundle_id))
        if is_complete:
            reconciled.append(bundle_id)
    return {"ok": True, "accepted": accepted, "duplicates": duplicates,
            "rejected": rejected, "reconciled_bundles": reconciled}
