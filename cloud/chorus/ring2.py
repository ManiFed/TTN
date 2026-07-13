#!/usr/bin/env python3
"""
CHORUS Ring 2 — structural evolution (CHORUS.md §7).

Ring 1 (cloud.chorus.backtest / cloud.tuning) already tunes the CHORUS
objective's ~15 scalar hyperparameters, gated by a deterministic backtest
replay so a plausible-but-wrong rationale never ships — no human approval
step, because the replay gate already IS the safety mechanism. Ring 2
extends that same discipline to STRUCTURAL changes: the per-family
cell-generation knobs cells.py used to hardcode as Python literals
(transient age-segment thresholds/multipliers, the band-cell value
multiplier, the LPV cadence floor) — "a class template revision... a
different cell layout for CVs" (CHORUS.md §7), now declarative data in
class_templates instead.

    propose_template()  → INSERT class_templates row, stage='advisory'.
                           (How a proposal gets here — hand-authored, or a
                           future LLM-advisory step per CHORUS.md §7 — is
                           deliberately out of scope here; this module only
                           owns validating and applying one, same as
                           cloud.tuning owns applying Ring-1 proposals
                           regardless of who authored the number.)
    run_pending()        → backtest every 'advisory' row via gate_template();
                           'live' on pass, 'rejected' (with the backtest
                           detail attached, so a failed proposal is always
                           inspectable) on fail. Nothing here waits on a
                           human — that's the point.
    active_templates()   → {family: params}, the newest 'live' row per
                           family — what cells.compile_cells actually reads.

Unlike a Ring-1 value_scale_* change (a pure multiplier on already-compiled
cells — backtest.gate() just rescales), a structural template change alters
how many cells exist and their relative shape, so it can only be tested by
recompiling cells.compile_cells from each archived run's raw T1 inputs
(backtest.archive_run's target_raw_by_id) — see _rebuild_with_template.

The deterministic core (assign.py's solver, cells.py's kernel math) never
changes shape; only cells.py's declarative per-family knobs do, and only
after clearing the same backtest bar Ring 1 already requires.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from cloud import db
from cloud.chorus import assign as assign_mod
from cloud.chorus import backtest
from cloud.chorus import cells as cellmod
from cloud.chorus import params as chorus_params

logger = logging.getLogger("cloud.chorus.ring2")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Declarative templates ───────────────────────────────────────────────────

def active_templates() -> dict:
    """{family: params} — the newest 'live' class_templates row per family.
    Empty/missing on any failure, so cells.compile_cells's hardcoded
    defaults are always a safe fallback: this function must never be the
    reason a plan fails to generate."""
    try:
        rows = db.query(
            "SELECT DISTINCT ON (family) family, params FROM class_templates "
            "WHERE stage = 'live' ORDER BY family, id DESC")
    except Exception as exc:
        logger.debug("active_templates unavailable: %s", exc)
        return {}
    return {r["family"]: db.loads(r.get("params"), {}) for r in rows}


def propose_template(family: str, params: dict, note: str = "") -> Optional[int]:
    """Stage a candidate structural revision for one family. Not applied
    until run_pending() backtests it — this call alone changes nothing
    cells.compile_cells sees."""
    now = _now()
    return db.execute(
        "INSERT INTO class_templates (family, params, stage, note, "
        " created_at, updated_at) VALUES (%s,%s,'advisory',%s,%s,%s)",
        (family, json.dumps(params), note, now, now), returning_id=True)


# ── Structural backtest (recompiles cells, not a value_scale rescale) ──────────

def _rebuild_with_template(inputs: dict, family_under_test: str,
                           candidate_params: dict) -> Optional[tuple]:
    """(contexts, opps_by_node, cells_by_target) for one archived run, with
    `family_under_test` compiled under `candidate_params` and every other
    family under today's live templates. None if this run predates
    target_raw_by_id/span_t0 (older archive rows aren't replayable this way
    — gate_template simply skips them, it doesn't error)."""
    if not inputs.get("target_raw") or not inputs.get("span_t0"):
        return None
    ch = chorus_params.merged(inputs.get("params") or {})
    span_t0 = datetime.fromisoformat(inputs["span_t0"])
    span_t1 = datetime.fromisoformat(inputs["span_t1"])
    band_union = set(inputs.get("band_union") or [])
    templates = dict(active_templates())
    templates[family_under_test] = candidate_params

    contexts = {nid: backtest._deser_ctx(nid, d)
               for nid, d in (inputs.get("contexts") or {}).items()}
    cells_by_target: dict = {}
    for tid, raw in inputs["target_raw"].items():
        try:
            cl = cellmod.compile_cells(
                raw["target"], raw.get("state") or {}, span_t0, span_t1,
                ch, float(raw.get("scarcity", 1.0)), band_union,
                templates=templates)
        except Exception as exc:
            logger.debug("Ring 2 cell recompile failed for %s: %s", tid, exc)
            continue
        if cl:
            cells_by_target[tid] = cl

    opps_by_node: dict = {nid: [] for nid in contexts}
    for d in inputs.get("opps") or []:
        o = backtest._deser_opp(d)
        if o.node_id in opps_by_node:
            opps_by_node[o.node_id].append(o)
    return contexts, opps_by_node, cells_by_target


def gate_template(family: str, candidate_params: dict, config: dict) -> tuple:
    """(allowed, detail): the same discipline as Ring 1's backtest.gate() —
    replay the archived runs under both the current live template and the
    candidate, require the candidate's realized coverage to be at least as
    good. With fewer than MIN_RUNS_TO_GATE replayable runs, unproven
    structural changes are NOT auto-applied (the opposite default from
    Ring 1's scalar gate, which allows through when unproven — a structural
    change alters cell shape for every future night it's live, a bigger
    blast radius than nudging one scalar, so this errs conservative until
    there's evidence)."""
    try:
        rows = db.query(
            """SELECT run_id, night, inputs, realized FROM chorus_run_archive
               WHERE realized IS NOT NULL AND realized != ''
               ORDER BY night DESC LIMIT 14""")
    except Exception as exc:
        return False, {"reason": f"archive unavailable ({exc})"}

    current_params = active_templates().get(family, {})
    totals = {"current": 0.0, "candidate": 0.0}
    n = 0
    for r in rows:
        inputs = db.loads(r.get("inputs"), {})
        realized = db.loads(r.get("realized"), {})
        if not realized:
            continue
        seed = int(inputs.get("seed", 0))
        ch = chorus_params.merged(inputs.get("params") or {})
        try:
            for tag, tmpl_params in (("current", current_params),
                                     ("candidate", candidate_params)):
                rebuilt = _rebuild_with_template(inputs, family, tmpl_params)
                if rebuilt is None:
                    raise ValueError("run predates structural replay support")
                contexts, opps, cells = rebuilt
                placements, _, _ = assign_mod.assign(
                    contexts, opps, cells, ch, seed=seed, local_search_ms=0.0)
                totals[tag] += backtest.realized_phi(placements, realized,
                                                     cells, contexts, ch)
            n += 1
        except Exception as exc:
            logger.debug("Ring 2 replay skipped for %s: %s", r.get("run_id"), exc)

    if n < backtest.MIN_RUNS_TO_GATE:
        return False, {"reason": f"only {n} replayable run(s) — "
                                 f"need {backtest.MIN_RUNS_TO_GATE}, not applied"}
    ok = totals["candidate"] >= totals["current"] * backtest.GATE_TOLERANCE
    detail = {"n_runs": n,
              "realized_phi_current": round(totals["current"], 4),
              "realized_phi_candidate": round(totals["candidate"], 4),
              "allowed": ok}
    logger.info("Ring 2 backtest (%s): %s (%s)", family,
               "PASS" if ok else "REJECT", detail)
    return ok, detail


def run_pending(config: dict, limit: int = 5) -> dict:
    """Background-loop entry point: backtest every pending proposal and
    apply or reject it — fully autonomous. A human never approves a
    template change; they can only ever review one that already failed
    (backtest_detail) or already shipped (stage='live', still reversible by
    proposing a revert)."""
    rows = db.query(
        "SELECT * FROM class_templates WHERE stage = 'advisory' "
        "ORDER BY id ASC LIMIT %s", (limit,))
    applied = rejected = 0
    for row in rows:
        params = db.loads(row.get("params"), {})
        try:
            ok, detail = gate_template(row["family"], params, config)
        except Exception as exc:
            ok, detail = False, {"error": str(exc)[:200]}
        stage = "live" if ok else "rejected"
        db.execute(
            "UPDATE class_templates SET stage = %s, backtest_detail = %s, "
            "updated_at = %s WHERE id = %s",
            (stage, json.dumps(detail), _now(), row["id"]))
        if ok:
            applied += 1
            logger.warning("Ring 2: class template #%s (%s) promoted to "
                           "live: %s", row["id"], row["family"], detail)
        else:
            rejected += 1
            logger.info("Ring 2: class template #%s (%s) rejected: %s",
                       row["id"], row["family"], detail)
    if applied or rejected:
        logger.info("Ring 2 pass: %d applied, %d rejected", applied, rejected)
    return {"applied": applied, "rejected": rejected}
