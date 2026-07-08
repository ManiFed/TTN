#!/usr/bin/env python3
"""
Nightly plan generation dispatch.

CHORUS is the primary scheduler.  This module keeps the shared persistence and
compatibility helpers that other planners use, then delegates plan generation to
CHORUS unless a config explicitly selects the older network optimizer.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from cloud import db
from src.shared_models import ObservationPlan

logger = logging.getLogger("cloud.scheduler")

STEP_MIN = 15                # slot granularity, minutes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Exposure selection ─────────────────────────────────────────────────────────

def choose_exposure(mag: Optional[float], node: dict) -> tuple:
    """
    (expDur seconds, expCount) sized to the target brightness and the node's
    actual hardware, not the Seestar's.

    Total integration scales with light grasp — a 100 mm aperture needs a
    quarter of the 50 mm baseline's time for the same SNR — clamped to
    [3, 30] minutes. Sub-exposures on alt-az mounts stay short (field
    rotation); equatorial mounts run subs up to their max_exposure_s.
    """
    max_exp = float(node.get("max_exposure_s", 30.0) or 30.0)
    aperture = float(node.get("aperture_mm", 50.0) or 50.0)
    mount = str(node.get("mount_type") or "alt_az")
    if mag is None:
        mag = 13.0
    if mag < 9.0:
        dur, total_min = 5.0, 5.0
    elif mag < 11.0:
        dur, total_min = 10.0, 8.0
    elif mag < 13.0:
        dur, total_min = 15.0, 12.0
    else:
        dur, total_min = 30.0, 20.0
    total_min = min(30.0, max(3.0, total_min * (50.0 / max(aperture, 10.0)) ** 2))
    if mount != "alt_az":
        # No field rotation: fewer, deeper subs (read noise amortizes).
        dur = max(dur, min(max_exp, 120.0))
    dur = min(dur, max_exp)
    count = max(5, int(round(total_min * 60.0 / dur)))
    return dur, count


# ── Plan generation ────────────────────────────────────────────────────────────

def _longitude_sep(a: float, b: float) -> float:
    diff = abs((a - b + 180.0) % 360.0 - 180.0)
    return min(diff, 360.0 - diff)


def generate_plan(
    node: dict,
    config: dict,
    coverage_reservations: dict[str, list[float]] | None = None,
) -> Optional[ObservationPlan]:
    """
    Build tonight's plan for one node. CHORUS is the default live algorithm.

    coverage_reservations is accepted for API compatibility with the archived
    greedy scheduler and ignored by CHORUS/network paths.
    """
    sched_cfg = config.get("scheduler", {})
    if sched_cfg.get("chorus", True):
        from cloud.chorus import planner as chorus_planner
        return chorus_planner.plan_single_node(node, config)

    if sched_cfg.get("network_optimizer"):
        # Coordinated network optimizer. For a single on-demand node this
        # degenerates to a per-node optimize (no cross-node state).
        from cloud import network_planner
        return network_planner.plan_single_node(node, config)

    logger.warning("No live scheduler enabled; CHORUS is the default scheduler")
    from cloud.chorus import planner as chorus_planner
    return chorus_planner.plan_single_node(node, config)


def _save_plan(plan: ObservationPlan) -> None:
    db.execute("UPDATE plans SET status = 'superseded' "
               "WHERE node_id = %s AND status = 'current'", (plan.node_id,))
    db.execute(
        """INSERT INTO plans (plan_id, node_id, night, generated_at, plan_json, status)
           VALUES (%s,%s,%s,%s,%s,'current')""",
        (plan.plan_id, plan.node_id, plan.night, plan.generated_at,
         json.dumps(plan.to_dict())),
    )


def current_plan(node_id: str) -> Optional[dict]:
    """The node's current plan as a dict, or None."""
    row = db.query_one(
        "SELECT plan_json FROM plans WHERE node_id = %s AND status = 'current' "
        "ORDER BY generated_at DESC LIMIT 1", (node_id,))
    return db.loads(row["plan_json"]) if row else None


def generate_all_plans(config: dict) -> int:
    """Generate a fresh plan for every online node. Returns plan count."""
    sched_cfg = config.get("scheduler", {})
    if sched_cfg.get("chorus", True):
        from cloud.chorus import planner as chorus_planner
        return chorus_planner.plan_network(config)

    if sched_cfg.get("network_optimizer"):
        from cloud import network_planner
        count = network_planner.plan_network(config)
        _maybe_shadow_chorus(config)
        return count

    logger.warning("No live scheduler enabled; CHORUS is the default scheduler")
    from cloud.chorus import planner as chorus_planner
    return chorus_planner.plan_network(config)


def _maybe_shadow_chorus(config: dict) -> None:
    """Legacy compatibility for callers that still ask for shadow CHORUS."""
    sched_cfg = config.get("scheduler", {})
    if not sched_cfg.get("chorus_shadow") or sched_cfg.get("chorus"):
        return
    try:
        from cloud.chorus import planner as chorus_planner
        chorus_planner.plan_shadow(config)
    except Exception as exc:
        logger.warning("CHORUS shadow run failed (non-fatal): %s", exc)
