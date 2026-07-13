#!/usr/bin/env python3
"""
CHORUS T3 — per-node sequencing and contingency ladders (CHORUS.md §6).

Sequencing keeps the incumbent machinery: observations ordered in time,
slew / filter-change / meridian-flip overhead from cloud.objective, meridian
side logic from cloud.network_planner.  What is new is the explanation payload
(expected information, delivery probability, predicted σ, the cells the
observation was bought for) and the contingency ladder: the top uncommitted
alternates for each node, valued against the *final* residual ledger, so an
upgraded node agent that gets clouded out or fails a solve can degrade to a
plan that is still globally coherent — without a cloud round-trip.  Old node
agents ignore the extra keys entirely.
"""

import logging
from typing import Optional

from cloud import objective
from cloud.chorus import assign as assign_mod
from cloud.conditions import angular_separation_deg
from cloud.network_planner import needs_meridian_flip
from src.shared_models import PlanItem

logger = logging.getLogger("cloud.chorus.perform")


def _top_cells(touches: list, limit: int = 3) -> list:
    ranked = sorted(touches, key=lambda t: t[0].nu * t[1], reverse=True)
    return [
        {"cell": cell.label or cell.cell_id, "kind": cell.kind,
         "rho": round(rho, 3), "nu": round(cell.nu, 3)}
        for cell, rho in ranked[:limit]
    ]


def sequence_node(ctx, placements: list, coord: dict) -> list:
    """Time-ordered PlanItems for one node's committed placements, with
    transition overhead reflected in the explanation."""
    ordered = sorted(placements, key=lambda p: p.slot)
    items: list = []
    prev = None
    prev_slot = 0
    for p in ordered:
        opp = p.opp
        se = opp.slots[p.slot]
        start_utc = ctx.slot_utc(p.slot)
        start_local = start_utc + ctx.utc_offset

        overhead_s = 0.0
        flip = False
        if prev is not None:
            sep = angular_separation_deg(prev.ra_deg, prev.dec_deg,
                                         opp.ra_deg, opp.dec_deg)
            az_from = prev.slots[prev_slot].az if prev_slot in prev.slots else 0.0
            flip = needs_meridian_flip(ctx.mount_type, az_from, se.az)
            overhead_s = objective.transition_overhead_seconds(
                sep, prev.filter != opp.filter, flip, coord)

        explanation = {
            "engine": "chorus",
            "scheduled_start_utc": start_utc.isoformat(),
            "scheduled_start_local": start_local.strftime("%H:%M"),
            "duration_minutes": opp.duration_min,
            "expected_info": round(p.marginal, 4),
            "p_deliver": round(p.p, 3),
            "p_sky": round(se.p_sky, 3),
            "sigma_pred": round(se.sigma, 4),
            "exposure": {"t_sub_s": opp.exposure.t_sub,
                         "n_sub": opp.exposure.n_sub,
                         "dwell_min": opp.exposure.dwell_min},
            "top_cells": _top_cells(p.touches),
            "transition_overhead_s": round(overhead_s, 1),
            "meridian_flip": flip,
            "tier": getattr(p, "tier", "core"),
        }
        items.append(PlanItem(
            target=opp.name,
            ra=round(opp.ra_deg / 15.0, 4),
            dec=round(opp.dec_deg, 4),
            expDur=opp.exposure.t_sub,
            expCount=opp.exposure.n_sub,
            binning=1,
            startTime=start_local.strftime("%H:%M"),
            target_id=opp.target_id,
            score=round(p.marginal, 4),
            filter=opp.filter,
            notes=(f"type={opp.target_type} mag={opp.mag} "
                   f"E[info]={p.marginal:.3f} p={p.p:.2f} "
                   f"sigma={se.sigma:.3f}"),
            explanation=explanation,
            observation_mode=opp.observation_mode,
            duration_minutes=(opp.duration_min
                              if opp.observation_mode == "time_series" else 0.0),
        ))
        prev, prev_slot = opp, p.slot

    items.sort(key=lambda i: i.startTime)
    return items


def contingency_ladder(ctx, node_opps: list, final_state,
                       cells_by_target: dict, params: dict,
                       top_k: int = 3) -> dict:
    """Fallback policy for one node: the best uncommitted opportunities
    valued against the *final* residual ledger — what the node should observe
    if a committed item fails (clouds, solve failure, saturation).

    Returned as plain dicts inside the plan JSON's additive `contingencies`
    key; legacy node agents ignore it (plans are parsed permissively)."""
    ranked = []
    for opp in node_opps:
        if opp.key in final_state.placed_keys:
            continue
        slot, val, _ = assign_mod.best_slot(opp, final_state,
                                            cells_by_target, params)
        if slot is None or val <= 0.0:
            continue
        ranked.append((val, slot, opp))
    ranked.sort(key=lambda r: (-r[0], r[2].seq))

    alternates = []
    for val, slot, opp in ranked[:top_k]:
        start_local = ctx.slot_utc(slot) + ctx.utc_offset
        start_hhmm = start_local.strftime("%H:%M")
        alternates.append({
            "target": opp.name,
            "target_id": opp.target_id,
            "ra": round(opp.ra_deg / 15.0, 4),
            "dec": round(opp.dec_deg, 4),
            "expDur": opp.exposure.t_sub,
            "expCount": opp.exposure.n_sub,
            "binning": 1,
            "filter": opp.filter,
            "earliestStart": start_hhmm,
            # Full schedule-item fields so an upgraded node agent can run an
            # alternate directly (gap fill / end-of-plan fill) without any
            # translation; legacy agents still ignore the whole block.
            "startTime": start_hhmm,
            "score": round(val, 4),
            "observation_mode": opp.observation_mode,
            "duration_minutes": (opp.duration_min
                                 if opp.observation_mode == "time_series"
                                 else 0.0),
            "notes": (f"alternate type={opp.target_type} mag={opp.mag} "
                      f"E[info]={val:.3f}"),
            "expected_info": round(val, 4),
            "trigger": "primary item failed, sky closed, or plan exhausted",
        })
    return {"alternates": alternates} if alternates else {}


