#!/usr/bin/env python3
"""
Live fleet — mid-night work reflow.

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
from datetime import datetime, timedelta, timezone
from typing import Optional

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
                "dark_streak": _consecutive_dark_nights(node_id),
            })
    return out


def detect_starved(config: dict) -> list[dict]:
    """Nodes sitting idle in the dark with nothing left to run.

    Unlike detect_dropouts (a node going dark-but-dead mid-plan), this is a
    node whose plan simply ran out — CHORUS under-filled its night. It only
    fires on the node's own explicit `work_starved` flag (see
    src.dashboard._mark_work_starved): idle+dark alone is not enough, since a
    node is briefly idle between every plan item as a matter of course.
    Skips any node already carrying a live 'topup' interrupt so a slow
    dispatch cycle can't pile a second one on top of the first.
    """
    fleet = live.fleet_state()
    now = _now()
    pending: set[str] = set()
    for r in db.query(
            "SELECT node_ids FROM interrupts WHERE reason = 'topup' "
            "AND expires_at > %s", (now,)):
        pending.update(db.loads(r.get("node_ids"), []))

    out: list[dict] = []
    for row in fleet:
        node_id = row["node_id"]
        if row["phase"] != "idle" or not row.get("is_dark") or not row.get("online"):
            continue
        if node_id in pending:
            continue
        detail = (live.node_live(node_id) or {}).get("detail") or {}
        if not detail.get("work_starved"):
            continue
        out.append({"node_id": node_id, "phase": "idle"})
    return out


def detect_urgent_alerts(config: dict) -> Optional[dict]:
    """New time-critical alert targets that arrived after tonight's plan was
    generated and haven't already been reflowed in.

    Rule-based urgency: a target qualifies when cloud.alerts marked it
    `time_critical` at ingestion (SN/TDE/GRB/nova/CV-outburst depending on
    source — see cloud.alerts.TYPE_PRIORITY) and it is bright enough for this
    fleet to usefully follow up. No LLM in this path — same "pure procedural
    hot path" contract as the rest of scoring/scheduling.

    Returns a dropout-shaped dict (node_id=None) so it can be fed straight
    into _candidate_placements/dispatch_reflow, or None if nothing qualifies.
    """
    reflow_cfg = (config.get("scheduler", {}) or {}).get("reflow_detect", {}) or {}
    mag_limit = float(reflow_cfg.get("urgent_alert_mag_limit", 18.0))
    night = _tonight()

    latest_plan = db.query_one(
        "SELECT MAX(generated_at) AS g FROM plans WHERE night = %s", (night,))
    since = (latest_plan or {}).get("g") or _now()

    already = {r["target_id"] for r in db.query(
        "SELECT target_id FROM reflow_log WHERE night = %s", (night,))}

    rows = db.query(
        """SELECT target_id, name, mag FROM targets
           WHERE active = 1 AND time_critical = 1 AND last_updated > %s""",
        (since,))
    remaining = [
        {"target_id": r["target_id"], "target": r["name"], "score": 0.0}
        for r in rows
        if r["target_id"] not in already
        and (r.get("mag") is None or float(r["mag"]) <= mag_limit)
    ]
    if not remaining:
        return None
    return {"node_id": None, "phase": "urgent_alert", "remaining": remaining}


def _remaining_items(node_id: str, current_idx) -> list[dict]:
    """Unexecuted items of a node's current plan (those after the live index).

    Excludes targets explicitly deactivated in the DB since the plan was
    generated: a node that hasn't been replanned for several nights (because
    it's been dark that whole time — see _consecutive_dark_nights) can be
    carrying a stale plan referencing a target that's since been deactivated
    (season ended, superseded). A fresh nightly replan would never offer
    that target to anyone; reflow shouldn't either. A target_id missing from
    `targets` entirely is kept (conservative — best_slot feasibility on
    candidate nodes is still the real gate); only a confirmed active=0 row
    excludes it.
    """
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
    candidate_ids = {it.get("target_id") for it in items[start:] if it.get("target_id")}
    if not candidate_ids:
        return []
    deactivated_ids = {r["target_id"] for r in db.query(
        "SELECT target_id FROM targets WHERE active = 0 AND target_id = ANY(%s)",
        (list(candidate_ids),))}
    remaining = []
    for it in items[start:]:
        tid = it.get("target_id")
        if tid and tid not in deactivated_ids:
            remaining.append({"target_id": tid, "target": it.get("target", ""),
                              "score": it.get("score", 0.0)})
    return remaining


def _topup_candidates(node_id: str) -> list[dict]:
    """Active targets a work-starved node hasn't already run or been offered
    tonight — the catalog scan _candidate_placements re-values for it.
    """
    night = _tonight()
    already = {r["target_id"] for r in db.query(
        "SELECT target_id FROM reflow_log WHERE night = %s", (night,))}
    plan = db.query_one(
        "SELECT plan_json FROM plans WHERE node_id = %s AND status = 'current' "
        "ORDER BY generated_at DESC LIMIT 1", (node_id,))
    used = set()
    if plan is not None:
        items = db.loads(plan.get("plan_json"), {}).get("items", []) or []
        used = {it.get("target_id") for it in items if it.get("target_id")}
    rows = db.query("SELECT target_id, name FROM targets WHERE active = 1")
    return [
        {"target_id": r["target_id"], "target": r["name"], "score": 0.0}
        for r in rows
        if r["target_id"] not in already and r["target_id"] not in used
    ]


# ── Multi-night escalation ──────────────────────────────────────────────────────

def _consecutive_dark_nights(node_id: str, max_lookback: int = 14) -> int:
    """How many consecutive nights, up to and including last night, this node
    produced zero measurements. A node still mid-dropout tonight that was
    *also* fully dark every prior night in this streak has been failing to
    deliver for a while — CHORUS's own nightly replan already routes fresh
    targets to whichever nodes are online, so nothing is structurally
    orphaned across nights, but reflow's own acceptance bar for THIS node's
    dropped work should relax as the streak grows: a marginal-value
    placement that's not quite worth dispatching for a one-off cloud-out is
    worth dispatching for a target that's been going unserved for days.
    """
    rows = db.query(
        "SELECT DISTINCT to_char(received_at::timestamptz AT TIME ZONE 'UTC', "
        "                       'YYYY-MM-DD') AS night "
        "FROM measurements WHERE node_id = %s "
        "AND received_at::timestamptz > (now() - (%s || ' days')::interval)",
        (node_id, max_lookback))
    observed_nights = {r["night"] for r in rows if r.get("night")}
    streak = 0
    day = datetime.now(timezone.utc).date()
    for _ in range(max_lookback):
        day = day - timedelta(days=1)
        if day.isoformat() in observed_nights:
            break
        streak += 1
    return streak


# ── Faithful CHORUS re-valuation (the greedy step, reused) ───────────────────────

def _candidate_placements(dropped: dict, config: dict,
                          eps_scale: float = 1.0,
                          candidate_nodes: Optional[set] = None) -> list[dict]:
    """Re-value the dropped node's remaining targets on currently-dark nodes.

    Returns [{to_node, target_id, target_name, expected_info}] — one entry per
    remaining target that a dark node can feasibly take, greedily de-conflicted
    across candidate nodes exactly as CHORUS's own greedy would.

    `eps_scale` relaxes the greedy step's min-marginal acceptance floor (see
    greedy_place) — used by tick() to widen acceptance for a node with a long
    dark_streak, since a marginal placement not quite worth dispatching for a
    one-off cloud-out is worth it for a target that's gone unserved for days.

    `candidate_nodes` overrides the default "every other dark node" set. Used
    by the work-starved top-up path, where the node needing the placement
    *is* the candidate — it has spare dark time, not a dead plan to evacuate.
    """
    from cloud.chorus import assign as assign_mod
    from cloud.chorus import cells as cellmod
    from cloud.chorus import horizon, ledger, ring2
    from cloud.chorus import params as chorus_params
    from cloud.network_planner import build_node_context
    from cloud import tuning

    remaining_ids = {r["target_id"] for r in dropped["remaining"]}
    if not remaining_ids:
        return []

    if candidate_nodes is not None:
        dark = set(candidate_nodes)
    else:
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
    class_templates = ring2.active_templates()
    cells_by_target: dict = {}
    ephemeris_by_target: dict = {}
    for t in targets:
        tid = t["target_id"]
        state = states.get(tid, {})
        try:
            sc = horizon.scarcity(t, fleet_rows, p_exec_by_node, clim, ch,
                                  today=span_t0)
            cl = cellmod.compile_cells(t, state, span_t0, span_t1, ch, sc,
                                       band_union, templates=class_templates)
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

    return greedy_place(contexts, opps_by_node, cells_by_target, ch, eps_scale=eps_scale)


def greedy_place(contexts: dict, opps_by_node: dict, cells_by_target: dict,
                 ch: dict, eps_scale: float = 1.0) -> list[dict]:
    """The CHORUS greedy step, scoped to the reflow subproblem.

    Repeatedly commits the globally-best (opportunity, slot) marginal — the exact
    `best_slot` valuation the contingency ladder uses — against a fresh residual
    ledger, at most one placement per target. Pure over its inputs so it can be
    unit-tested with synthetic CHORUS objects (no weather/DB).

    `eps_scale` < 1.0 relaxes the min-marginal acceptance floor below — see
    _candidate_placements for why (multi-night dropout escalation).
    """
    from cloud.chorus import assign as assign_mod

    state = assign_mod._State(contexts, cells_by_target,
                              float(ch.get("same_site_repeat_factor", 0.25)), params=ch)
    eps = float(ch.get("min_marginal", 0.02)) * max(0.0, min(1.0, eps_scale))
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
                    config: dict, dark_streak: int = 0,
                    reason: Optional[str] = None) -> int:
    """Turn reflow placements into targeted interrupts + audit rows + pushes.

    `reason` distinguishes a work-starved top-up ('topup') from the default
    dropout evacuation. Left as None, the interrupt reads 'reflow' (the
    node-facing wording, unchanged) and the audit row reads 'dropout' (the
    reflow_log default) — both exactly as before this parameter existed.
    """
    night = _tonight()
    expires = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    interrupt_reason = reason or "reflow"
    log_reason = reason or "dropout"
    n = 0
    for pl in placements:
        try:
            import json
            iid = db.execute(
                """INSERT INTO interrupts
                       (target_id, name, ra_deg, dec_deg, mag, reason, node_ids,
                        created_at, expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (pl["target_id"], pl["target_name"], float(pl["ra_deg"]),
                 float(pl["dec_deg"]), pl.get("mag"), interrupt_reason,
                 json.dumps([pl["to_node"]]), _now(), expires),
                returning_id=True)
            db.execute(
                """INSERT INTO reflow_log
                       (night, from_node, to_node, target_id, target_name,
                        expected_info, interrupt_id, dark_streak, reason,
                        created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (night, dropped_node, pl["to_node"], pl["target_id"],
                 pl["target_name"], pl["expected_info"], iid, dark_streak,
                 log_reason, _now()))
            live.publish(pl["to_node"], "interrupt",
                         {"reason": interrupt_reason, "target_id": pl["target_id"]})
            n += 1
        except Exception as exc:
            logger.warning("reflow dispatch failed for %s→%s: %s",
                           dropped_node, pl.get("to_node"), exc)
    if n:
        logger.info("Reflow: %d item(s) moved off %s (dark_streak=%d)",
                    n, dropped_node, dark_streak)
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


def _effective_cap(config: dict) -> int:
    """Tonight's reflow dispatch cap, self-graduating from reconcile_outcomes
    history instead of a single static number an operator has to raise by
    hand once they trust the system.

    Purely a function of reflow_log — no persisted "current stage" — so a
    config change or a run of bad nights is reflected on the very next
    tick(), not stuck until someone manually resets a flag. Starts at
    reflow_grad_start_cap (small blast radius while unproven) and doubles
    (reflow_grad_step_factor) toward the configured ceiling
    (reflow_max_per_night) for every reflow_grad_min_nights of clean history
    beyond the minimum; drops straight back to the start cap the moment the
    trailing window's delivery rate falls below reflow_grad_min_delivery_rate.
    Set scheduler.reflow_auto_grade: false to disable this and use the
    static ceiling directly, as before.
    """
    sched = config.get("scheduler", {}) or {}
    ceiling = int(sched.get("reflow_max_per_night", 200))
    if not bool(sched.get("reflow_auto_grade", True)):
        return ceiling
    start = int(sched.get("reflow_grad_start_cap", 5))
    min_nights = max(1, int(sched.get("reflow_grad_min_nights", 3)))
    min_rate = float(sched.get("reflow_grad_min_delivery_rate", 0.7))
    lookback_nights = int(sched.get("reflow_grad_lookback_nights", 14))
    step_factor = max(1.0, float(sched.get("reflow_grad_step_factor", 2.0)))

    since = (datetime.now(timezone.utc) - timedelta(days=lookback_nights)).isoformat()
    rows = db.query(
        "SELECT night, outcome FROM reflow_log "
        "WHERE outcome IN ('delivered', 'missed') AND created_at >= %s", (since,))
    nights_seen = {r["night"] for r in rows}
    if len(nights_seen) < min_nights or not rows:
        return min(start, ceiling)
    rate = sum(1 for r in rows if r["outcome"] == "delivered") / len(rows)
    if rate < min_rate:
        return min(start, ceiling)

    grades = 1 + (len(nights_seen) - min_nights) // min_nights
    cap = start
    for _ in range(grades):
        cap = int(cap * step_factor)
    return max(start, min(cap, ceiling))


def tick(config: dict) -> int:
    """One reflow pass. Gated by scheduler.reflow. Returns items reflowed."""
    if not (config.get("scheduler", {}) or {}).get("reflow", False):
        return 0
    cap = _effective_cap(config)
    if _reflows_tonight() >= cap:
        return 0
    sched_cfg = config.get("scheduler", {}) or {}
    streak_threshold = int(sched_cfg.get("reflow_streak_threshold", 2))
    streak_eps_relax = float(sched_cfg.get("reflow_streak_eps_relax", 0.5))

    total = 0
    for dropped in detect_dropouts(config):
        streak = dropped.get("dark_streak", 0)
        eps_scale = streak_eps_relax if streak >= streak_threshold else 1.0
        try:
            placements = _candidate_placements(dropped, config, eps_scale=eps_scale)
        except Exception as exc:
            logger.warning("reflow valuation failed for %s: %s",
                           dropped["node_id"], exc)
            incidents.log(dropped["node_id"], "reflow_failed", severity="warning",
                          detail={"error": str(exc)[:200]})
            continue
        if placements:
            total += dispatch_reflow(dropped["node_id"], placements, config,
                                     dark_streak=streak)

    if _reflows_tonight() < cap:
        urgent = detect_urgent_alerts(config)
        if urgent:
            try:
                placements = _candidate_placements(urgent, config)
            except Exception as exc:
                logger.warning("reflow valuation failed for urgent alerts: %s", exc)
                incidents.log("fleet", "reflow_alert_failed", severity="warning",
                              detail={"error": str(exc)[:200]})
                placements = []
            if placements:
                total += dispatch_reflow("urgent_alert", placements, config)

    for starved in detect_starved(config):
        if _reflows_tonight() >= cap:
            break
        node_id = starved["node_id"]
        remaining = _topup_candidates(node_id)
        if not remaining:
            continue
        try:
            placements = _candidate_placements(
                {"node_id": node_id, "remaining": remaining}, config,
                candidate_nodes={node_id})
        except Exception as exc:
            logger.warning("reflow valuation failed for starved %s: %s",
                           node_id, exc)
            incidents.log(node_id, "reflow_topup_failed", severity="warning",
                          detail={"error": str(exc)[:200]})
            continue
        if placements:
            total += dispatch_reflow(node_id, placements, config, reason="topup")
    return total
