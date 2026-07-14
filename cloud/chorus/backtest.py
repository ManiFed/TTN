#!/usr/bin/env python3
"""
CHORUS Ring-1 backtest gate (CHORUS.md §7).

Because the planner is deterministic and each run's opportunity inputs are
archived (chorus_run_archive), any candidate hyperparameter set θ′ can be
*replayed* over the last N nights' actual inputs and scored against what
really happened — which (node, target) pairs delivered accepted data, and at
what σ.  A proposed change to the "chorus" tuning group is applied only if its
backtested realized coverage is at least as good as the incumbent's.

The LLM proposes; arithmetic disposes.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from cloud import db
from cloud.chorus import assign as assign_mod
from cloud.chorus import cells as cellmod
from cloud.chorus import params as chorus_params

logger = logging.getLogger("cloud.chorus.backtest")

MIN_RUNS_TO_GATE = 3       # below this, the trust region alone governs
GATE_TOLERANCE = 0.995     # candidate must reach ≥ 99.5% of incumbent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Serialization ─────────────────────────────────────────────────────────────

def _ser_opp(o) -> dict:
    return {
        "node_id": o.node_id, "target_id": o.target_id, "name": o.name,
        "ra": o.ra_deg, "dec": o.dec_deg, "mag": o.mag, "type": o.target_type,
        "t_sub": o.exposure.t_sub, "n_sub": o.exposure.n_sub,
        "dwell": o.exposure.dwell_min, "sigma": o.exposure.sigma,
        "need": o.need,
        "slots": {str(s): [round(se.p_sky, 4), round(se.sigma, 5),
                           round(se.alt, 2), round(se.az, 1)]
                  for s, se in o.slots.items()},
        "filter": o.filter, "p_exec": o.p_exec, "p_accept": o.p_accept,
        "explore": o.explore, "lon": o.node_lon, "variant": o.variant,
        "mode": o.observation_mode, "dur": o.duration_min, "seq": o.seq,
        "eph": o.ephemeris,
    }


def _deser_opp(d: dict):
    from cloud.chorus.physics import ExposurePlan
    return assign_mod.ChorusOpportunity(
        node_id=d["node_id"], target_id=d["target_id"], name=d["name"],
        ra_deg=float(d["ra"]), dec_deg=float(d["dec"]),
        mag=(float(d["mag"]) if d.get("mag") is not None else None),
        target_type=d.get("type", "unknown"),
        exposure=ExposurePlan(t_sub=float(d["t_sub"]), n_sub=int(d["n_sub"]),
                              dwell_min=float(d["dwell"]),
                              sigma=float(d["sigma"])),
        need=int(d["need"]),
        slots={int(s): assign_mod.SlotEval(p_sky=v[0], sigma=v[1],
                                           alt=v[2], az=v[3])
               for s, v in (d.get("slots") or {}).items()},
        filter=d.get("filter", "CV"), p_exec=float(d["p_exec"]),
        p_accept=float(d["p_accept"]), explore=float(d.get("explore", 0.0)),
        node_lon=float(d.get("lon", 0.0)), variant=d.get("variant", "epoch"),
        observation_mode=d.get("mode", "single_epoch"),
        duration_min=float(d.get("dur", 0.0)), seq=int(d.get("seq", 0)),
        ephemeris=d.get("eph"))


def _ser_ctx(ctx) -> dict:
    return {
        "t0": ctx.t0.isoformat(), "t1": ctx.t1.isoformat(),
        "n_slots": ctx.n_slots,
        "utc_offset_h": ctx.utc_offset.total_seconds() / 3600.0,
        "min_alt": ctx.min_alt, "mount_type": ctx.mount_type,
        "filters": ctx.filters, "max_targets": ctx.max_targets,
        "lat": ctx.lat, "lon": ctx.lon,
    }


def _deser_ctx(node_id: str, d: dict):
    from datetime import timedelta
    from cloud.network_planner import NodeContext
    return NodeContext(
        node={}, node_id=node_id, lat=float(d.get("lat", 0.0)),
        lon=float(d.get("lon", 0.0)),
        t0=datetime.fromisoformat(d["t0"]), t1=datetime.fromisoformat(d["t1"]),
        n_slots=int(d["n_slots"]),
        utc_offset=timedelta(hours=float(d.get("utc_offset_h", 0.0))),
        min_alt=float(d.get("min_alt", 25.0)), horizon_mask=[],
        filters=list(d.get("filters") or ["CV"]), cooled=False,
        mount_type=d.get("mount_type", "alt_az"), cloud_relax=0.0,
        max_targets=int(d.get("max_targets", 12)))


def _cell_family(cell, target_types: dict) -> str:
    if cell.kind == "event":
        return "exoplanet"
    return cellmod.family_of(target_types.get(cell.target_id, ""))


def archive_run(night: str, seed: int, contexts: dict, opps_by_node: dict,
                cells_by_target: dict, ch_params: dict, phi_expected: float,
                target_names: dict, target_types: dict,
                shadow: bool = False, target_raw_by_id: Optional[dict] = None,
                band_union: Optional[set] = None,
                span_t0: Optional[datetime] = None,
                span_t1: Optional[datetime] = None) -> None:
    """Persist one run's full deterministic inputs for later replay.
    Best-effort — never fails plan generation.

    target_raw_by_id — {target_id: {"target": targets-row dict, "state":
    chorus_target_state dict, "scarcity": float}}, the raw T1 inputs behind
    each target's already-compiled cells. Ring 1's gate() only ever needs to
    RESCALE archived cells (a value_scale_* change is a pure multiplier), but
    a Ring 2 structural template change (cloud/chorus/ring2.py) alters how
    many cells exist and their relative shape — that can only be tested by
    recompiling cells.compile_cells from these raw inputs, not by rescaling
    the already-compiled ones. Optional and additive: existing Ring-1-only
    callers/replays are unaffected when omitted.
    """
    try:
        forecast_by_node = {}
        for nid, opps in opps_by_node.items():
            skies = [se.p_sky for o in opps for se in o.slots.values()]
            forecast_by_node[nid] = (round(sum(skies) / len(skies), 4)
                                     if skies else None)
        inputs = {
            "seed": seed,
            "contexts": {nid: _ser_ctx(c) for nid, c in contexts.items()},
            "cells": {tid: [dict(c.to_dict(),
                                 family=_cell_family(c, target_types))
                            for c in cl]
                      for tid, cl in cells_by_target.items()},
            "opps": [_ser_opp(o) for lst in opps_by_node.values() for o in lst],
            "params": ch_params,
            "forecast_by_node": forecast_by_node,
            "target_names": target_names,
            "target_raw": target_raw_by_id or {},
            "band_union": sorted(band_union or set()),
            "span_t0": span_t0.isoformat() if span_t0 else None,
            "span_t1": span_t1.isoformat() if span_t1 else None,
        }
        db.execute(
            """INSERT INTO chorus_run_archive
                   (run_id, night, ran_at, inputs, phi_expected, shadow)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (f"chorus_{uuid.uuid4().hex[:10]}", night, _now(),
             json.dumps(inputs), round(phi_expected, 4), 1 if shadow else 0))
    except Exception as exc:
        logger.warning("Run archive failed (non-fatal): %s", exc)


# ── Replay + realized scoring ─────────────────────────────────────────────────

def _rebuild(inputs: dict, candidate: dict) -> tuple:
    """(contexts, opps_by_node, cells_by_target) from an archived run, with
    cell values rescaled from the archived value_scale_* to the candidate's
    so class-priority changes are actually exercised by the replay."""
    archived = chorus_params.merged(inputs.get("params") or {})
    contexts = {nid: _deser_ctx(nid, d)
                for nid, d in (inputs.get("contexts") or {}).items()}
    cells_by_target: dict = {}
    for tid, cl in (inputs.get("cells") or {}).items():
        out = []
        for d in cl:
            cell = cellmod.InfoCell.from_dict(d)
            fam = d.get("family", "default")
            old_vs = max(chorus_params.value_scale(archived, fam), 1e-6)
            cell.nu = cell.nu / old_vs * chorus_params.value_scale(candidate, fam)
            out.append(cell)
        cells_by_target[tid] = out
    opps_by_node: dict = {nid: [] for nid in contexts}
    for d in inputs.get("opps") or []:
        o = _deser_opp(d)
        if o.node_id in opps_by_node:
            opps_by_node[o.node_id].append(o)
    return contexts, opps_by_node, cells_by_target


def realized_phi(placements: list, realized: dict, cells_by_target: dict,
                 contexts: dict, params: dict) -> float:
    """Score a plan against what actually happened: only placements whose
    (node, target) delivered accepted data count, at their realized σ, with
    delivery probability 1 (it happened)."""
    R = {c.cell_id: max(0.0, min(1.0, c.residual))
         for cl in cells_by_target.values() for c in cl}
    total = 0.0
    ordered = sorted(placements,
                     key=lambda p: (p.node_id, p.slot, p.opp.target_id))
    for p in ordered:
        real = (realized.get(p.node_id) or {}).get(p.opp.target_id)
        if not real or not real.get("accepted"):
            continue
        sigma = real.get("sigma")
        ctx = contexts[p.node_id]
        start, end = assign_mod._dwell_bounds(p.opp, p.slot, ctx)
        from cloud.chorus import physics
        for cell in cells_by_target.get(p.opp.target_id, []):
            k = cellmod.kernel(cell, start, end, p.opp.filter,
                               p.opp.ephemeris, params)
            if k < 1e-4:
                continue
            s = float(sigma) if sigma else p.opp.slots[p.slot].sigma
            rho = physics.capture(s, cell.sigma_ref) * k
            total += cell.nu * R[cell.cell_id] * rho
            R[cell.cell_id] *= (1.0 - rho)
    return total


def gate(current_chorus: dict, candidate_chorus: dict, config: dict) -> tuple:
    """(allowed, detail): replay the archived runs under both parameter sets
    and require the candidate's realized coverage to be at least as good.
    With fewer than MIN_RUNS_TO_GATE realized runs, the trust region alone
    governs (allowed)."""
    try:
        rows = db.query(
            """SELECT run_id, night, inputs, realized FROM chorus_run_archive
               WHERE realized IS NOT NULL AND realized != ''
               ORDER BY night DESC LIMIT 14""")
    except Exception as exc:
        return True, {"reason": f"archive unavailable ({exc})"}

    usable = [r for r in rows if db.loads(r.get("realized"), {})]
    if len(usable) < MIN_RUNS_TO_GATE:
        return True, {"reason": f"only {len(usable)} realized runs — not gated"}

    cur = chorus_params.merged(current_chorus)
    new = chorus_params.merged(candidate_chorus)
    totals = {"current": 0.0, "candidate": 0.0}
    n = 0
    for r in usable:
        inputs = db.loads(r.get("inputs"), {})
        realized = db.loads(r.get("realized"), {})
        seed = int(inputs.get("seed", 0))
        try:
            for tag, theta in (("current", cur), ("candidate", new)):
                contexts, opps, cells = _rebuild(inputs, theta)
                placements, _, _ = assign_mod.assign(
                    contexts, opps, cells, theta, seed=seed,
                    local_search_ms=0.0)     # greedy-only: fast, deterministic
                totals[tag] += realized_phi(placements, realized, cells,
                                            contexts, theta)
            n += 1
        except Exception as exc:
            logger.warning("Backtest replay failed for %s: %s",
                           r.get("run_id"), exc)
    if n == 0:
        return True, {"reason": "no replayable runs — not gated"}
    ok = totals["candidate"] >= totals["current"] * GATE_TOLERANCE
    detail = {"n_runs": n,
              "realized_phi_current": round(totals["current"], 4),
              "realized_phi_candidate": round(totals["candidate"], 4),
              "allowed": ok}
    logger.info("Chorus backtest gate: %s (%s)",
                "PASS" if ok else "REJECT", detail)
    return ok, detail
