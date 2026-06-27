#!/usr/bin/env python3
"""Reliability incident logging for node health and scheduler trust."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

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
