#!/usr/bin/env python3
"""Reliability incident logging for node health and scheduler trust."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cloud import db

logger = logging.getLogger("cloud.incidents")

_ATTRIBUTION_BY_TYPE = {
    "borderline_photometry": "node",
    "device_disconnect": "node",
    "image_processing_failed": "node",
    "measurement_storage_failed": "system",
    "measurement_validation_failed": "node",
    "photometry_failed": "node",
    "plate_solve_failed": "node",
    "raw_image_rejected": "node",
    "slew_failed": "node",
    "clouds_blocked_observation": "environment",
    "poor_seeing": "environment",
    "weather_hold": "environment",
    "plan_generation_failed": "system",
    "scheduler_error": "system",
    "api_outage": "system",
}

_SEVERITY_WEIGHT = {
    "info": 0.5,
    "warning": 1.0,
    "error": 1.5,
    "critical": 2.0,
}

_ATTRIBUTION_WEIGHT = {
    "node": 0.06,
    "environment": 0.018,
    "system": 0.0,
    "unknown": 0.025,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify(incident_type: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify an incident by scheduler attribution and trust impact."""
    incident_type = (incident_type or "unknown").strip()
    detail = detail or {}
    attribution = _ATTRIBUTION_BY_TYPE.get(incident_type)

    if attribution is None:
        lower = f"{incident_type} {json.dumps(detail, sort_keys=True)}".lower()
        if any(term in lower for term in ("cloud", "rain", "seeing", "weather", "wind")):
            attribution = "environment"
        elif any(term in lower for term in ("api", "database", "scheduler", "plan_generation")):
            attribution = "system"
        elif any(term in lower for term in (
            "plate", "solve", "disconnect", "device", "photometry", "raw_image", "slew"
        )):
            attribution = "node"
        else:
            attribution = "unknown"

    return {
        "incident_type": incident_type,
        "attribution": attribution,
        "scheduler_relevant": attribution in ("node", "environment", "unknown"),
    }


def recent_scheduler_penalty(node_id: str, lookback_days: int = 14) -> float:
    """Recent incident penalty applied to scheduler trust, capped conservatively."""
    if not node_id:
        return 0.0
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    rows = db.query(
        """SELECT incident_type, severity FROM reliability_incidents
           WHERE node_id = %s AND occurred_at >= %s AND resolved_at IS NULL""",
        (node_id, cutoff.isoformat()),
    )
    penalty = 0.0
    for row in rows:
        cls = classify(row.get("incident_type", ""))
        attribution = cls["attribution"]
        severity = str(row.get("severity") or "info").lower()
        penalty += _ATTRIBUTION_WEIGHT.get(attribution, 0.0) * _SEVERITY_WEIGHT.get(severity, 1.0)
    return round(max(0.0, min(0.45, penalty)), 4)


def log(
    node_id: str,
    incident_type: str,
    *,
    severity: str = "info",
    target_name: str = "",
    measurement_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record an operational incident without letting logging failure affect hot paths."""
    if not node_id:
        return
    try:
        detail = dict(detail or {})
        detail.setdefault("classification", classify(incident_type, detail))
        db.execute(
            """INSERT INTO reliability_incidents
                   (node_id, incident_type, severity, target_name, measurement_id,
                    detail, occurred_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (
                node_id,
                incident_type[:80],
                severity[:24],
                target_name[:160],
                measurement_id,
                json.dumps(detail),
                _now(),
            ),
        )
    except Exception as exc:
        logger.warning("Could not record reliability incident for %s: %s", node_id, exc)


# ── Structured incident lifecycle ──────────────────────────────────────────────

def open_incident(node_id: str, title: str, root_cause: str = "unknown",
                  severity: str = "warning", trigger_event: str = "") -> Optional[int]:
    """
    Open a new structured incident for a node.

    Returns the incident id, or None if a non-resolved incident already exists
    for this node (prevents duplicate floods).
    """
    if not node_id:
        return None
    try:
        existing = db.query_one(
            "SELECT id FROM incidents WHERE node_id = %s AND status IN ('open','investigating')",
            (node_id,),
        )
        if existing:
            return existing["id"]

        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        raw = db.query_one(
            "SELECT COUNT(*) AS n FROM reliability_incidents WHERE node_id=%s AND occurred_at>%s",
            (node_id, cutoff_24h),
        ) or {}
        n_raw = int((raw or {}).get("n", 0) or 0)

        now = _now()
        return db.execute(
            """INSERT INTO incidents
                   (node_id, status, title, root_cause, severity,
                    opened_at, updated_at, trigger_event, n_raw_events)
               VALUES (%s,'open',%s,%s,%s,%s,%s,%s,%s)""",
            (node_id, title[:200], root_cause[:40], severity[:24],
             now, now, trigger_event[:80], n_raw),
            returning_id=True,
        )
    except Exception as exc:
        logger.warning("Could not open incident for %s: %s", node_id, exc)
        return None


def resolve_incident(incident_id: int, resolver: str = "",
                     note: str = "") -> None:
    """Mark a structured incident as resolved."""
    try:
        now = _now()
        db.execute(
            """UPDATE incidents SET status='resolved', resolved_at=%s, updated_at=%s,
                    resolver=%s, resolution_note=%s
               WHERE id=%s""",
            (now, now, resolver[:120], note[:500], incident_id),
        )
    except Exception as exc:
        logger.warning("Could not resolve incident %s: %s", incident_id, exc)


def auto_triage(node_id: str, stats: dict[str, Any]) -> None:
    """
    Auto-open a structured incident based on freshly computed performance stats.

    Called by registry.refresh_node_performance() after every nightly update.
    Conditions that trigger an incident (only one open incident per node):
      - outlier_rate > 0.40  →  cross-validation quality degradation
      - reliability_score < 0.30  →  composite reliability collapse
      - ≥3 error/critical raw events in the last 24 h  →  operational errors
    """
    if not node_id:
        return

    outlier_rate      = float(stats.get("outlier_rate", 0) or 0)
    reliability       = float(stats.get("reliability_score", 1) or 1)
    incident_penalty  = float(stats.get("incident_penalty", 0) or 0)

    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    raw_errors = db.query_one(
        """SELECT COUNT(*) AS n FROM reliability_incidents
           WHERE node_id=%s AND severity IN ('error','critical') AND occurred_at>%s""",
        (node_id, cutoff_24h),
    ) or {}
    n_errors = int((raw_errors or {}).get("n", 0) or 0)

    title, root_cause, severity = None, "unknown", "warning"

    if n_errors >= 3:
        title      = f"Repeated operational errors on {node_id} (last 24 h)"
        root_cause = "software"
        severity   = "error"
    elif outlier_rate > 0.40:
        title      = f"High cross-validation outlier rate on {node_id} ({outlier_rate:.0%})"
        root_cause = "optics"
        severity   = "warning"
    elif reliability < 0.30:
        title      = f"Low reliability score on {node_id} ({reliability:.2f})"
        root_cause = "unknown"
        severity   = "warning"

    if title:
        iid = open_incident(node_id, title, root_cause, severity,
                            trigger_event="auto_triage")
        if iid:
            logger.warning("Incident #%d opened for %s: %s", iid, node_id, title)
