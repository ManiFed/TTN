#!/usr/bin/env python3
"""
THE ORGANISM — mid-night work reflow.

CHORUS plans once per night. When a node then clouds out or drops offline with
committed work still ahead of it, that science is simply lost until the next
nightly replan. Reflow moves it: it takes the dropped node's unexecuted targets
and re-values them on the nodes that are dark right now, dispatching the best
placements as targeted interrupts over the realtime bus.

It does NOT reinvent the scheduler. Valuation reuses CHORUS's own machinery —
`build_opportunities` for the candidate nodes and the exact `best_slot` marginal
the contingency ladder uses — against a fresh residual ledger for just the
remaining targets. This is the greedy step of CHORUS run on the reflow
subproblem, nothing more; assign.py/cells.py/ledger.py are untouched.

Dispatch is the existing interrupt path (never preempts an active exposure,
enforced node-side). Guardrails: only nodes the live map says are dark, a
per-night reflow cap, and best_slot returning None when a candidate node has no
feasible window means it is simply skipped.
"""

import logging
from datetime import datetime, timezone

from cloud import db, incidents, live, registry

logger = logging.getLogger("cloud.chorus.reflow")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tonight() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Dropout detection ───────────────────────────────────────────────────────────

def detect_dropouts(config: dict) -> list[dict]:
    """Nodes that have gone dark-but-dead mid-night with work still pending.

    A dropout is a node whose live state is offline/clouded/parked while it
    still holds unexecuted items in its current plan. Pure DB logic — the live
    map (populated by heartbeats) is the single source of truth for phase.
    """
    reflow_cfg = (config.get("scheduler", {}) or {}).get("reflow_detect", {}) or {}
    dead_phases = set(reflow_cfg.get("dead_phases",
                                     ["offline", "clouded", "parked"]))
    fleet = {n["node_id"]: n for n in live.fleet_state()}
    dead_ids = {nid for nid, ls in fleet.items()
                if ls["phase"] in dead_phases}
    # Self-healing: a node with an open CRITICAL incident (emergency park,
    # device disconnect, disk exhaustion) is effectively down even if its live
    # phase hasn't caught up — fold those in as dropouts too.
    try:
        for r in db.query(
                "SELECT DISTINCT node_id FROM incidents "
                "WHERE status = 'open' AND severity = 'critical'"):
            dead_ids.add(r["node_id"])
    except Exception as exc:
        logger.debug("incident dropout scan skipped: %s", exc)

    out: list[dict] = []
    for node_id in dead_ids:
        ls = fleet.get(node_id, {})
        remaining = _remaining_items(node_id, ls.get("plan_item_idx"))
        if remaining:
            out.append({
                "node_id": node_id,
                "phase": ls.get("phase", "incident"),
                "remaining": remaining,
            })
    return out


def _remaining_items(node_id: str, current_idx) -> list[dict]:
    """Unexecuted items of a node's current plan (those after the live index)."""
    plan = db.query_one(
        "SELECT plan_json FROM plans WHERE node_id = %s AND status = 'current' "
        "ORDER BY generated_at DESC LIMIT 1", (node_id,))
    if plan is None:
        return []
    items = db.loads(plan.get("plan_json"), {}).get("items", []) or []
    # Everything strictly after the item the node was last seen working. When
    # the index is unknown, treat the whole plan as still pending (conservative:
    # reflow will still be gated by best_slot feasibility on candidate nodes).
    start = (current_idx + 1) if isinstance(current_idx, int) and current_idx >= 0 else 0
    remaining = []
    for it in items[start:]:
        tid = it.get("target_id")
        if tid:
            remaining.append({"target_id": tid, "target": it.get("target", ""),
                              "score": it.get("score", 0.0)})
    return remaining


# ── Faithful CHORUS re-valuation (the greedy step, reused) ───────────────────────

def _candidate_placements(dropped: dict, config: dict) -> list[dict]:
    """Re-value the dropped node's remaining targets on currently-dark nodes.

    Returns [{to_node, target_id, target_name, expected_info}] — one entry per
    remaining target that a dark node can feasibly take, greedily de-conflicted
    across candidate nodes exactly as CHORUS's own greedy would.
    """
    from cloud.chorus import assign as assign_mod
    from cloud.chorus import cells as cellmod
    from cloud.chorus import horizon, ledger
    from cloud.chorus import params as chorus_params
    from cloud.network_planner import build_node_context
    from cloud import tuning

    remaining_ids = {r["target_id"] for r in dropped["remaining"]}
    if not remaining_ids:
        return []

    dark = live.dark_online_nodes() - {dropped["node_id"]}
    if not dark:
        return []

    ch = chorus_params.merged(tuning.active_params(config).get("chorus"))

    # Contexts for the dark candidate nodes only.
    contexts: dict = {}
    node_by_id: dict = {}
    for node in registry.list_nodes():
        if node["node_id"] not in dark or node.get("status") == "disabled":
            continue
        try:
            ctx = build_node_context(node, config)
        except Exception as exc:
            logger.debug("reflow context build failed for %s: %s",
                         node["node_id"], exc)
            ctx = None
        if ctx is not None:
            contexts[ctx.node_id] = ctx
            node_by_id[ctx.node_id] = node
    if not contexts:
        return []

    span_t0 = min(c.t0 for c in contexts.values())
    span_t1 = max(c.t1 for c in contexts.values())
    band_union = {f for c in contexts.values() for f in c.filters}

    vecs = ledger.vectors_for(list(node_by_id.values()))
    cals = ledger.site_calibrations()
    clim = ledger.climatology_fn()
    states = ledger.target_states()
    p_exec_by_node = {nid: v["p_exec"] for nid, v in vecs.items()}
    fleet_rows = list(node_by_id.values())

    # Compile cells for the remaining targets only.
    targets = [t for t in db.query("SELECT * FROM targets WHERE active = 1")
               if t["target_id"] in remaining_ids]
    cells_by_target: dict = {}
    ephemeris_by_target: dict = {}
    for t in targets:
        tid = t["target_id"]
        state = states.get(tid, {})
        try:
            sc = horizon.scarcity(t, fleet_rows, p_exec_by_node, clim, ch,
                                  today=span_t0)
            cl = cellmod.compile_cells(t, state, span_t0, span_t1, ch, sc,
                                       band_union)
        except Exception as exc:
            logger.debug("reflow cell compile failed for %s: %s", t.get("name"), exc)
            continue
        if cl:
            cells_by_target[tid] = cl
            eph = state.get("ephemeris") or {}
            if eph.get("period_days"):
                ephemeris_by_target[tid] = eph
    if not cells_by_target:
        return []

    # Opportunities per candidate node, then the CHORUS greedy step on a fresh
    # residual ledger scoped to these targets.
    opps_by_node: dict = {}
    seq = 0
    for nid, ctx in contexts.items():
        try:
            opps = assign_mod.build_opportunities(
                ctx, node_by_id[nid], vecs.get(nid, {}), cals.get(nid), targets,
                cells_by_target, ch, config,
                ephemeris_by_target=ephemeris_by_target, seq_start=seq)
        except Exception as exc:
            logger.debug("reflow opportunity build failed for %s: %s", nid, exc)
            opps = []
        seq += len(opps) + 1
        opps_by_node[nid] = opps

    return greedy_place(contexts, opps_by_node, cells_by_target, ch)


def greedy_place(contexts: dict, opps_by_node: dict, cells_by_target: dict,
                 ch: dict) -> list[dict]:
    """The CHORUS greedy step, scoped to the reflow subproblem.

    Repeatedly commits the globally-best (opportunity, slot) marginal — the exact
    `best_slot` valuation the contingency ladder uses — against a fresh residual
    ledger, at most one placement per target. Pure over its inputs so it can be
    unit-tested with synthetic CHORUS objects (no weather/DB).
    """
    from cloud.chorus import assign as assign_mod

    state = assign_mod._State(contexts, cells_by_target,
                              float(ch.get("same_site_repeat_factor", 0.25)))
    eps = float(ch.get("min_marginal", 0.02))
    placed: dict = {}   # target_id -> placement dict
    remaining_opps = [opp for opps in opps_by_node.values() for opp in opps]
    while remaining_opps:
        best = None
        for opp in remaining_opps:
            if opp.target_id in placed:
                continue
            slot, val, tl = assign_mod.best_slot(opp, state, cells_by_target, ch)
            if slot is None or val < eps:
                continue
            if best is None or val > best[1]:
                best = (opp, val, slot, tl)
        if best is None:
            break
        opp, val, slot, tl = best
        state.commit(assign_mod.Placement(
            node_id=opp.node_id, opp=opp, slot=slot, marginal=val,
            p=assign_mod.delivery_p(opp, slot), touches=tl))
        placed[opp.target_id] = {
            "to_node": opp.node_id,
            "target_id": opp.target_id,
            "target_name": opp.name,
            "ra_deg": opp.ra_deg,
            "dec_deg": opp.dec_deg,
            "mag": opp.mag,
            "expected_info": round(val, 4),
        }
        remaining_opps = [o for o in remaining_opps if o.target_id != opp.target_id]
    return list(placed.values())


# ── Dispatch ────────────────────────────────────────────────────────────────────

def dispatch_reflow(dropped_node: str, placements: list[dict],
                    config: dict) -> int:
    """Turn reflow placements into targeted interrupts + audit rows + pushes."""
    from datetime import timedelta
    night = _tonight()
    expires = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    n = 0
    for pl in placements:
        try:
            import json
            iid = db.execute(
                """INSERT INTO interrupts
                       (target_id, name, ra_deg, dec_deg, mag, reason, node_ids,
                        created_at, expires_at)
                   VALUES (%s,%s,%s,%s,%s,'reflow',%s,%s,%s)""",
                (pl["target_id"], pl["target_name"], float(pl["ra_deg"]),
                 float(pl["dec_deg"]), pl.get("mag"),
                 json.dumps([pl["to_node"]]), _now(), expires),
                returning_id=True)
            db.execute(
                """INSERT INTO reflow_log
                       (night, from_node, to_node, target_id, target_name,
                        expected_info, interrupt_id, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (night, dropped_node, pl["to_node"], pl["target_id"],
                 pl["target_name"], pl["expected_info"], iid, _now()))
            live.publish(pl["to_node"], "interrupt",
                         {"reason": "reflow", "target_id": pl["target_id"]})
            n += 1
        except Exception as exc:
            logger.warning("reflow dispatch failed for %s→%s: %s",
                           dropped_node, pl.get("to_node"), exc)
    if n:
        logger.info("Reflow: %d item(s) moved off %s", n, dropped_node)
    return n


def _reflows_tonight() -> int:
    return int((db.query_one(
        "SELECT COUNT(*) AS n FROM reflow_log WHERE night = %s",
        (_tonight(),)) or {}).get("n") or 0)


def reconcile_outcomes(config: dict, lookback_hours: float = 18.0) -> dict:
    """Mark whether reflowed work actually got delivered.

    For each dispatched reflow, check if the receiving node produced a
    measurement after the reflow. Writes reflow_log.outcome ('delivered' /
    'missed') — a data feed the nightly CHORUS ledger join can read as a
    realization signal. Does NOT touch the ledger math itself.
    """
    from datetime import timedelta
    since = (datetime.now(timezone.utc)
             - timedelta(hours=lookback_hours)).isoformat()
    rows = db.query(
        "SELECT * FROM reflow_log WHERE outcome = 'dispatched' "
        "AND created_at > %s", (since,))
    delivered = missed = 0
    for r in rows:
        hit = db.query_one(
            "SELECT 1 FROM measurements m JOIN targets t "
            "  ON t.name = m.target_name "
            "WHERE m.node_id = %s AND t.target_id = %s AND m.received_at > %s "
            "LIMIT 1",
            (r["to_node"], r["target_id"], r["created_at"]))
        outcome = "delivered" if hit else "missed"
        db.execute("UPDATE reflow_log SET outcome = %s WHERE id = %s",
                   (outcome, r["id"]))
        delivered += hit is not None
        missed += hit is None
    if rows:
        logger.info("Reflow reconcile: %d delivered, %d missed", delivered, missed)
    return {"delivered": delivered, "missed": missed}


def tick(config: dict) -> int:
    """One reflow pass. Gated by scheduler.reflow. Returns items reflowed."""
    if not (config.get("scheduler", {}) or {}).get("reflow", False):
        return 0
    cap = int((config.get("scheduler", {}) or {}).get("reflow_max_per_night", 200))
    if _reflows_tonight() >= cap:
        return 0
    total = 0
    for dropped in detect_dropouts(config):
        try:
            placements = _candidate_placements(dropped, config)
        except Exception as exc:
            logger.warning("reflow valuation failed for %s: %s",
                           dropped["node_id"], exc)
            incidents.log(dropped["node_id"], "reflow_failed", severity="warning",
                          detail={"error": str(exc)[:200]})
            continue
        if placements:
            total += dispatch_reflow(dropped["node_id"], placements, config)
    return total
