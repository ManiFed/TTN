#!/usr/bin/env python3
"""
CHORUS orchestration — the nightly pipeline (CHORUS.md §6).

    T0  ledger state (read here, written by ledger.run_nightly)
    T1  scarcity sweep + cell compilation, per target
    T2  opportunity generation + fleet-wide assignment
    T3  per-node sequencing + contingency ladders → ObservationPlans

Enabled by scheduler.chorus; scheduler.generate_all_plans delegates here when
the flag is on (falling back to the network optimizer / legacy packer when
off, same instant-rollback contract as scheduler.network_optimizer).
Shadow mode (scheduler.chorus_shadow) runs the full pipeline and records
telemetry + the backtest archive without saving plans — the staged-rollout
path from CHORUS.md §10.

Everything here is deterministic: the assignment seed derives from the night
date, and all learned inputs (ledger vectors, calibrations, target state,
tuned hyperparameters) are read from the DB as plain values.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from cloud import db, incidents, objective, registry, tuning
from cloud.chorus import assign as assign_mod
from cloud.chorus import backtest, cells as cellmod, horizon, ledger, perform
from cloud.chorus import params as chorus_params
from cloud.network_planner import build_node_context
from cloud.scheduler import _save_plan
from src.shared_models import ObservationPlan

logger = logging.getLogger("cloud.chorus.planner")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan(config: dict, nodes: list, save: bool = True,
          shadow: bool = False) -> tuple:
    """Build, assign, sequence and (optionally) persist plans for `nodes`.
    Returns (plans_by_node, stats)."""
    all_params = tuning.active_params(config)
    ch = chorus_params.merged(all_params.get("chorus"))
    coord = all_params.get("coordination", objective.DEFAULT_COORD_PARAMS)

    # ── Contexts (dark windows, capability) ───────────────────────────────────
    contexts: dict = {}
    node_by_id: dict = {}
    for node in nodes:
        if node.get("status") == "disabled":
            continue
        try:
            ctx = build_node_context(node, config)
        except Exception as exc:
            logger.error("Context build failed for %s: %s",
                         node.get("node_id"), exc)
            incidents.log(node.get("node_id", "?"), "plan_generation_failed",
                          severity="error", detail={"error": str(exc)})
            continue
        if ctx is None:
            continue
        contexts[ctx.node_id] = ctx
        node_by_id[ctx.node_id] = node
    if not contexts:
        return {}, {"n_assignments": 0, "final_phi": 0.0, "greedy_phi": 0.0}

    span_t0 = min(c.t0 for c in contexts.values())
    span_t1 = max(c.t1 for c in contexts.values())
    band_union = {f for c in contexts.values() for f in c.filters}

    # ── T0 reads: ledger vectors, calibrations, target state ─────────────────
    vecs = ledger.vectors_for(list(node_by_id.values()))
    cals = ledger.site_calibrations()
    clim = ledger.climatology_fn()
    states = ledger.target_states()
    p_exec_by_node = {nid: v["p_exec"] for nid, v in vecs.items()}

    targets = db.query("SELECT * FROM targets WHERE active = 1")

    # ── T1: scarcity + cell compilation ───────────────────────────────────────
    fleet_rows = list(node_by_id.values())
    cells_by_target: dict = {}
    ephemeris_by_target: dict = {}
    target_names: dict = {}
    target_types: dict = {}
    for t in targets:
        tid = t["target_id"]
        state = states.get(tid, {})
        try:
            s = horizon.scarcity(t, fleet_rows, p_exec_by_node, clim, ch,
                                 today=span_t0)
            cl = cellmod.compile_cells(t, state, span_t0, span_t1, ch, s,
                                       band_union)
        except Exception as exc:
            logger.warning("Cell compile failed for %s: %s", t.get("name"), exc)
            continue
        if not cl or sum(c.nu * c.residual for c in cl) <= 1e-6:
            continue
        cells_by_target[tid] = cl
        target_names[tid] = t["name"]
        target_types[tid] = t.get("target_type", "unknown")
        eph = (state.get("ephemeris") or {})
        if eph.get("period_days"):
            ephemeris_by_target[tid] = eph

    # ── T2: opportunities + assignment ────────────────────────────────────────
    opps_by_node: dict = {}
    seq = 0
    for nid, ctx in contexts.items():
        node = node_by_id[nid]
        try:
            opps = assign_mod.build_opportunities(
                ctx, node, vecs.get(nid, {}), cals.get(nid), targets,
                cells_by_target, ch, config,
                ephemeris_by_target=ephemeris_by_target, seq_start=seq)
        except Exception as exc:
            logger.error("Opportunity build failed for %s: %s", nid, exc)
            incidents.log(nid, "plan_generation_failed", severity="error",
                          detail={"error": str(exc)})
            opps = []
        seq += len(opps) + 1
        opps_by_node[nid] = opps

    # Transit event cells registered during generation need names/types too.
    for tid in cells_by_target:
        if tid not in target_names:
            first = next((c for c in cells_by_target[tid]), None)
            target_names[tid] = (first.label.split(" ")[0]
                                 if first and first.label else tid)
            target_types[tid] = "EXOPLANET"

    seed = int(span_t0.strftime("%Y%m%d"))
    budget_ms = float(config.get("scheduler", {}).get("local_search_ms", 1500))
    placements, residuals, stats = assign_mod.assign(
        contexts, opps_by_node, cells_by_target, ch,
        seed=seed, local_search_ms=budget_ms)

    # ── T3: sequencing + contingencies → plans ────────────────────────────────
    final_state = assign_mod.replay(contexts, cells_by_target, placements, ch)
    by_node: dict = {nid: [] for nid in contexts}
    for p in final_state.placements:
        by_node[p.node_id].append(p)

    plans_by_node: dict = {}
    for nid, ctx in contexts.items():
        items = perform.sequence_node(ctx, by_node.get(nid, []), coord)
        contingencies = perform.contingency_ladder(
            ctx, opps_by_node.get(nid, []), final_state, cells_by_target, ch)
        night_local = (ctx.t0 + ctx.utc_offset).strftime("%Y-%m-%d")
        plan = ObservationPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:10]}",
            node_id=nid,
            night=night_local,
            generated_at=_now(),
            items=items,
            contingencies=contingencies,
        )
        if save:
            _save_plan(plan)
        plans_by_node[nid] = plan
        logger.info("CHORUS plan %s for %s: %d targets, %d alternates%s",
                    plan.plan_id, nid, len(items),
                    len(contingencies.get("alternates", [])),
                    " (shadow)" if shadow else "")

    # ── Telemetry + backtest archive ─────────────────────────────────────────
    stats["engine"] = "chorus"
    stats["shadow"] = shadow
    _record_run(contexts, final_state, cells_by_target, stats)
    night_utc = span_t0.strftime("%Y-%m-%d")
    backtest.archive_run(night_utc, seed, contexts, opps_by_node,
                         cells_by_target, ch, stats.get("final_phi", 0.0),
                         target_names, target_types, shadow=shadow)
    return plans_by_node, stats


def _record_run(contexts: dict, final_state, cells_by_target: dict,
                stats: dict) -> None:
    """plan_runs telemetry: coverage of the compiled cell value, expected
    deliveries, redundancy.  Best-effort."""
    try:
        placements = final_state.placements
        groups: dict = {}
        for p in placements:
            groups.setdefault(p.opp.target_id, []).append(p)
        n_targets = len(groups)
        n_obs = len(placements)
        total_nu = sum(c.nu for cl in cells_by_target.values() for c in cl)
        captured = sum(
            c.nu * (max(0.0, min(1.0, c.residual))
                    - final_state.R.get(c.cell_id, c.residual))
            for cl in cells_by_target.values() for c in cl)
        coverage = round(captured / total_nu, 4) if total_nu > 0 else 0.0
        import json as _json
        db.execute(
            """INSERT INTO plan_runs
                   (run_id, ran_at, n_nodes, n_targets, n_assignments,
                    objective_value, greedy_objective, redundancy_rate,
                    cadence_fill, stats)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (f"run_{uuid.uuid4().hex[:10]}", _now(), len(contexts),
             n_targets, n_obs,
             stats.get("final_phi", 0.0), stats.get("greedy_phi", 0.0),
             round(n_obs / n_targets, 3) if n_targets else 0.0,
             coverage, _json.dumps(stats)))
    except Exception as exc:
        logger.warning("plan_runs telemetry write failed: %s", exc)


# ── Public entry points (same shape as network_planner's) ─────────────────────

def plan_network(config: dict) -> int:
    """Generate fresh CHORUS plans for the whole fleet.  Returns plan count."""
    nodes = registry.list_nodes()
    plans_by_node, stats = _plan(config, nodes, save=True)
    logger.info("CHORUS network plan: %d nodes, %d assignments, "
                "Φ %.3f (greedy %.3f), E[deliveries] %.1f",
                len(plans_by_node), stats.get("n_assignments", 0),
                stats.get("final_phi", 0.0), stats.get("greedy_phi", 0.0),
                stats.get("expected_deliveries", 0.0))
    return len(plans_by_node)


def plan_single_node(node: dict, config: dict) -> Optional[ObservationPlan]:
    """On-demand plan for one node (no cross-node residual sharing available,
    exactly like network_planner.plan_single_node)."""
    plans_by_node, _ = _plan(config, [node], save=True)
    return plans_by_node.get(node["node_id"])


def plan_shadow(config: dict) -> int:
    """Full CHORUS run with telemetry + archive but no saved plans — the
    shadow stage of the staged rollout.  Returns the would-be plan count."""
    nodes = registry.list_nodes()
    plans_by_node, _ = _plan(config, nodes, save=False, shadow=True)
    return len(plans_by_node)
