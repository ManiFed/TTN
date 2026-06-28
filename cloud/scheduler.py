#!/usr/bin/env python3
"""
Nightly plan generation — turns scored targets into a concrete observation
plan per node, in the exact JSON shape the Node Agent schedule runner
consumes (target / ra hours / dec / expDur / expCount / binning / startTime).

For each node:
  1. compute tonight's dark window at the node's exact location
  2. take its scored targets above the score threshold, best first
  3. for each, find the altitude window above the node's minimum altitude
  4. greedily pack targets into free time slots inside their windows,
     observing each as close to its transit (highest altitude) as the
     remaining free time allows
  5. choose exposure settings from target brightness and the node's
     field-rotation exposure cap
  6. emit startTime in the node's local clock (utc_offset_hours)

generate_all_plans() runs on the afternoon/regular loop; generate_plan() is
also called for a single node on demand.
"""

import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from cloud import db, incidents, registry
from cloud.conditions import altitude_curve, night_window
from cloud.transit_windows import get_tonight_transits
from src.shared_models import ObservationPlan, PlanItem

logger = logging.getLogger("cloud.scheduler")

SLEW_OVERHEAD_MIN = 5.0      # slew + settle + plate-solve centering per target
STEP_MIN = 15                # slot granularity, minutes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Exposure selection ─────────────────────────────────────────────────────────

def choose_exposure(mag: Optional[float], node: dict) -> tuple:
    """
    (expDur seconds, expCount) sized to the target brightness and capped by
    the node's per-sub exposure limit (alt-az field rotation). Total time on
    target stays in the 5–20 min range that suits stacked Seestar photometry.
    """
    max_exp = float(node.get("max_exposure_s", 30.0))
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
    dur = min(dur, max_exp)
    count = max(5, int(round(total_min * 60.0 / dur)))
    return dur, count


# ── Plan generation ────────────────────────────────────────────────────────────

def _longitude_sep(a: float, b: float) -> float:
    diff = abs((a - b + 180.0) % 360.0 - 180.0)
    return min(diff, 360.0 - diff)


def _reserved_nearby(
    reservations: dict[str, list[float]] | None,
    target_id: str,
    lon: float,
    min_sep_deg: float,
) -> bool:
    if not reservations or not target_id:
        return False
    return any(_longitude_sep(lon, other) < min_sep_deg for other in reservations.get(target_id, []))


def generate_plan(
    node: dict,
    config: dict,
    coverage_reservations: dict[str, list[float]] | None = None,
) -> Optional[ObservationPlan]:
    """
    Build tonight's plan for one node from its stored scores.
    Returns the plan (possibly with zero items), or None when there is no
    darkness at the node within 24 h.
    """
    sched_cfg = config.get("scheduler", {})
    if sched_cfg.get("network_optimizer"):
        # New path: coordinated network optimizer. For a single on-demand node
        # this degenerates to a per-node optimize (no cross-node state), still
        # richer than the legacy greedy packer below.
        from cloud import network_planner
        return network_planner.plan_single_node(node, config)

    min_score = float(sched_cfg.get("min_score", 0.25))
    max_targets = int(sched_cfg.get("max_targets_per_night", 12))
    sun_limit = float(sched_cfg.get("sun_altitude_limit", -12.0))
    min_longitude_sep = float(sched_cfg.get("longitude_diversity_deg", 45.0))
    diversity_bypass_score = float(sched_cfg.get("longitude_diversity_bypass_score", 0.82))

    night = night_window(node["latitude"], node["longitude"],
                         sun_limit_deg=sun_limit)
    if night is None:
        logger.info("No darkness for %s within 24 h — no plan", node["node_id"])
        return None
    t0, t1 = night

    rows = db.query(
        """SELECT s.total, s.components, t.* FROM scores s
           JOIN targets t ON t.target_id = s.target_id
           WHERE s.node_id = %s AND t.active = 1 AND s.total >= %s
           ORDER BY s.total DESC LIMIT 60""",
        (node["node_id"], min_score),
    )

    # Occupancy grid over the night in STEP_MIN slots
    n_slots = max(1, int((t1 - t0).total_seconds() / 60 / STEP_MIN))
    free = [True] * n_slots
    min_alt = float(node.get("min_altitude_deg", 25.0))
    utc_offset = timedelta(hours=float(node.get("utc_offset_hours", 0.0)))

    items: list[PlanItem] = []

    # ── Phase 1: lock in transit windows ──────────────────────────────────────
    try:
        transit_windows = get_tonight_transits(
            t0, t1,
            lat_deg=node["latitude"],
            lon_deg=node["longitude"],
            min_alt_deg=min_alt,
        )
    except Exception as exc:
        logger.warning("Transit window lookup failed: %s", exc)
        transit_windows = []

    for tw in transit_windows:
        if len(items) >= max_targets:
            break
        obs_min = (tw.obs_end_utc - tw.obs_start_utc).total_seconds() / 60
        need = max(1, math.ceil(obs_min / STEP_MIN))
        start_offset = (tw.obs_start_utc - t0).total_seconds() / 60
        s0 = max(0, int(start_offset / STEP_MIN))
        if s0 + need > n_slots:
            continue
        if not all(free[s0:s0 + need]):
            continue  # another transit already claimed these slots

        for s in range(s0, s0 + need):
            free[s] = False

        # Transit photometry: short exposures, continuous, ~30s cadence
        exp_dur = min(float(node.get("max_exposure_s", 30.0)), 30.0)
        exp_count = max(10, int(obs_min * 60 / exp_dur))
        start_local = tw.obs_start_utc + utc_offset
        items.append(PlanItem(
            target=tw.name,
            ra=round(tw.ra_deg / 15.0, 4),
            dec=round(tw.dec_deg, 4),
            expDur=exp_dur,
            expCount=exp_count,
            binning=1,
            startTime=start_local.strftime("%H:%M"),
            target_id=tw.target_id,
            score=0.85,
            filter=(node.get("filters") or "CV").split(",")[0].strip(),
            notes=(f"type=EXOPLANET mag={tw.mag:.1f} "
                   f"depth={tw.depth_ppt:.1f}ppt "
                   f"dur={tw.duration_hours:.2f}h "
                   f"mid={tw.t_mid_utc.strftime('%H:%M')}UTC"),
            observation_mode="time_series",
            duration_minutes=round(obs_min, 1),
        ))
        logger.info("Transit locked: %s  mid=%s  window=%.0f min",
                    tw.name, tw.t_mid_utc.strftime("%H:%M"), obs_min)

    # ── Phase 2: greedily pack remaining slots with scored targets ─────────────
    scored_ids = {item.target_id for item in items}
    for row in rows:
        if len(items) >= max_targets:
            break
        if row["target_id"] in scored_ids:
            continue
        if (
            float(row["total"]) < diversity_bypass_score
            and _reserved_nearby(
                coverage_reservations,
                row["target_id"],
                float(node["longitude"]),
                min_longitude_sep,
            )
        ):
            continue

        exp_dur, exp_count = choose_exposure(row["mag"], node)
        duration_min = exp_dur * exp_count / 60.0 + SLEW_OVERHEAD_MIN
        need = max(1, math.ceil(duration_min / STEP_MIN))
        if need > n_slots:
            continue

        curve = altitude_curve(row["ra_deg"], row["dec_deg"],
                               node["latitude"], node["longitude"],
                               t0, t1, step_min=STEP_MIN)
        alts = [a for _, a in curve]

        best_start, best_mean = None, -1.0
        for s in range(0, n_slots - need + 1):
            window = alts[s:s + need]
            if len(window) < need or min(window) < min_alt:
                continue
            if not all(free[s:s + need]):
                continue
            mean_alt = sum(window) / need
            if mean_alt > best_mean:
                best_start, best_mean = s, mean_alt
        if best_start is None:
            continue

        for s in range(best_start, best_start + need):
            free[s] = False

        start_utc = t0 + timedelta(minutes=best_start * STEP_MIN)
        start_local = start_utc + utc_offset
        comp = db.loads(row["components"], {})
        explanation = comp.get("explanation") or {}
        explanation.update({
            "scheduled_start_utc": start_utc.isoformat(),
            "scheduled_start_local": start_local.strftime("%H:%M"),
            "duration_minutes": round(duration_min, 1),
            "longitude_diversity": {
                "min_separation_deg": min_longitude_sep,
                "bypass_score": diversity_bypass_score,
            },
        })
        items.append(PlanItem(
            target=row["name"],
            ra=round(row["ra_deg"] / 15.0, 4),
            dec=round(row["dec_deg"], 4),
            expDur=exp_dur,
            expCount=exp_count,
            binning=1,
            startTime=start_local.strftime("%H:%M"),
            target_id=row["target_id"],
            score=float(row["total"]),
            filter=(node.get("filters") or "CV").split(",")[0].strip(),
            notes=f"type={row['target_type']} mag={row['mag']} "
                  f"best_alt={comp.get('best_alt_deg', '?')}",
            explanation=explanation,
        ))
        if coverage_reservations is not None:
            coverage_reservations.setdefault(row["target_id"], []).append(float(node["longitude"]))

    # Execute in time order — the node runner walks the list sequentially
    items.sort(key=lambda i: i.startTime)

    night_local = (t0 + utc_offset).strftime("%Y-%m-%d")
    plan = ObservationPlan(
        plan_id=f"plan_{uuid.uuid4().hex[:10]}",
        node_id=node["node_id"],
        night=night_local,
        generated_at=_now(),
        items=items,
    )
    _save_plan(plan)
    logger.info("Plan %s for %s: %d targets over %s → %s UTC",
                plan.plan_id, node["node_id"], len(items),
                t0.strftime("%H:%M"), t1.strftime("%H:%M"))
    return plan


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
    if config.get("scheduler", {}).get("network_optimizer"):
        from cloud import network_planner
        return network_planner.plan_network(config)

    count = 0
    reservations: dict[str, list[float]] = {}
    nodes = sorted(
        registry.list_nodes(),
        key=lambda n: (float(n.get("longitude", 0.0)) + 360.0) % 360.0,
    )
    for node in nodes:
        if node.get("status") == "disabled":
            continue
        try:
            if generate_plan(node, config, reservations) is not None:
                count += 1
        except Exception as exc:
            logger.error("Plan generation failed for %s: %s", node["node_id"], exc)
            incidents.log(
                node["node_id"],
                "plan_generation_failed",
                severity="error",
                detail={"error": str(exc)},
            )
    return count
