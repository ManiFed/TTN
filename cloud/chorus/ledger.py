#!/usr/bin/env python3
"""
CHORUS T0 — the ledger: reliability vectors, target state, Ring-0 calibration
(CHORUS.md §4, §7 Ring 0).

Node reliability is measured where it acts, as conjugate posteriors updated by
counting (no optimizer, no LLM), with a ~60-night exponential half-life so
nodes can redeem themselves and regressions surface quickly:

    p_exec   ~ Beta   plan item on a workable night → data delivered
                      (weather/system-attributed incidents excluded via the
                      existing incident classifier; node-attributed incidents
                      add failure pseudo-counts)
    p_accept ~ Beta   delivered → AAVSO-submitted, non-outlier, good/acceptable
    kappa    ~ shrunk ratio of delivered variance to the physics prediction —
                      hardware-normalized skill (a Seestar at its physical
                      limit reads ~1; a miscollimated C8 reads ≫1)

Priors are Beta(4,2) (mean 0.67, the principled version of "start at 0.5"),
and the posterior widths feed the exploration bonus, so new nodes get real,
low-regret work sized to shrink their own uncertainty.

Also here: per-target science state (phase-coverage residuals, CV hazard
clocks, outburst flags), per-site weather-forecast calibration (two-parameter
logistic fit by deterministic grid search over archived forecast-vs-realized
pairs), and monthly clear-sky climatology for the horizon sweep.
"""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from cloud import db, incidents
from cloud.chorus import cells as cellmod
from cloud.chorus import physics

logger = logging.getLogger("cloud.chorus.ledger")

PRIOR_A = 4.0                # Beta prior successes
PRIOR_B = 2.0                # Beta prior failures
HALF_LIFE_NIGHTS = 60.0      # evidence decay
LOOKBACK_DAYS = 120
KAPPA_PRIOR_WEIGHT = 8.0     # pseudo-observations at kappa = 1
KAPPA_BOUNDS = (0.5, 25.0)
SEASON_HALF_LIFE_D = 60.0    # phase-coverage residual refresh toward 1.0
OUTBURST_DELTA_MAG = 1.0     # brighter than catalog by this → outburst state

# Chronic-dropout quarantine: a night where cloud.chorus.reflow's dropout
# detection had to move this node's own plan items elsewhere is real
# execution-failure signal that a plain zero-delivery night can't supply on
# its own (see the p_exec comment in refresh_node — weather and node fault
# are otherwise indistinguishable without more signal). Repeated across
# several nights, that signal escalates: a node that keeps needing reflow
# has demonstrated it, not the weather, is the recurring problem.
DROPOUT_FAIL_WEIGHT = 0.5             # exec_b pseudo-count per dropout night
DROPOUT_STREAK_THRESHOLD = 3          # distinct dropout nights (in LOOKBACK_DAYS) to quarantine
DROPOUT_QUARANTINE_MULTIPLIER = 2.0   # extra weight once the streak crosses the threshold


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decay(age_days: float) -> float:
    return 0.5 ** (max(0.0, age_days) / HALF_LIFE_NIGHTS)


def _beta_stats(a: float, b: float) -> tuple:
    mean = a / (a + b)
    var = (a * b) / ((a + b) ** 2 * (a + b + 1.0))
    return mean, math.sqrt(max(var, 0.0))


# ── Node reliability vector ───────────────────────────────────────────────────

def _default_vector() -> dict:
    mean, sd = _beta_stats(PRIOR_A, PRIOR_B)
    return {"p_exec": round(mean, 4), "p_accept": round(mean, 4),
            "kappa": 1.0, "explore": round(2 * sd + 0.3, 4),
            "sd_exec": round(sd, 4), "sd_accept": round(sd, 4)}


def node_vector(node_id: str) -> dict:
    """The node's reliability vector (posterior means + exploration scalar),
    from the ledger table; Beta(4,2)/κ=1 priors when the node is new."""
    try:
        row = db.query_one(
            "SELECT * FROM chorus_node_ledger WHERE node_id = %s", (node_id,))
    except Exception:
        row = None
    if not row:
        return _default_vector()
    p_exec, sd_e = _beta_stats(float(row["p_exec_a"]), float(row["p_exec_b"]))
    p_acc, sd_a = _beta_stats(float(row["p_accept_a"]), float(row["p_accept_b"]))
    n_k = int(row.get("n_kappa", 0) or 0)
    sd_k = 1.0 / math.sqrt(KAPPA_PRIOR_WEIGHT + n_k)
    return {
        "p_exec": round(p_exec, 4), "p_accept": round(p_acc, 4),
        "kappa": float(row.get("kappa", 1.0) or 1.0),
        "explore": round(sd_e + sd_a + 0.5 * sd_k, 4),
        "sd_exec": round(sd_e, 4), "sd_accept": round(sd_a, 4),
    }


def vectors_for(nodes: list) -> dict:
    return {n["node_id"]: node_vector(n["node_id"]) for n in nodes}


def _accepted(m: dict) -> bool:
    return (int(m.get("aavso_submitted") or 0) == 1
            and m.get("validation_status") != "outlier"
            and m.get("quality_flag") in ("good", "acceptable"))


def refresh_node(node: dict) -> dict:
    """Recompute one node's ledger from delivered outcomes + incidents.
    Deterministic counting with exponential decay; called nightly."""
    node_id = node["node_id"]
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()

    meas = db.query(
        """SELECT target_name, magnitude, uncertainty, airmass, quality_flag,
                  validation_status, aavso_submitted, received_at
           FROM measurements WHERE node_id = %s AND received_at >= %s""",
        (node_id, since))
    plans = db.query(
        "SELECT night, plan_json FROM plans "
        "WHERE node_id = %s AND generated_at >= %s", (node_id, since))

    def age_d(iso: str) -> float:
        try:
            return (now - datetime.fromisoformat(iso)).total_seconds() / 86400.0
        except (TypeError, ValueError):
            return LOOKBACK_DAYS

    delivered_by_night: dict = {}
    for m in meas:
        night = (m.get("received_at") or "")[:10]
        delivered_by_night.setdefault(night, set()).add(m.get("target_name"))

    # p_exec: on workable nights (node delivered something), how many planned
    # items actually produced data.  Nights with zero delivery are excluded —
    # we can't separate clouds from failure without more signal, and the
    # incident classifier supplies the node-attributed failures below.
    exec_a, exec_b = PRIOR_A, PRIOR_B
    seen_nights = set()
    for p in plans:
        night = p.get("night") or ""
        if night in seen_nights:
            continue
        seen_nights.add(night)
        delivered = delivered_by_night.get(night)
        if not delivered:
            continue
        items = db.loads(p.get("plan_json"), {}).get("items", [])
        names = {(it or {}).get("target") for it in items if (it or {}).get("target")}
        if not names:
            continue
        w = _decay(age_d(night + "T12:00:00+00:00"))
        hits = len(names & delivered)
        exec_a += w * hits
        exec_b += w * max(0, len(names) - hits)

    # Node-attributed incidents are execution failures wherever they occurred.
    try:
        rows = db.query(
            """SELECT incident_type, detail, occurred_at FROM reliability_incidents
               WHERE node_id = %s AND occurred_at >= %s""", (node_id, since))
    except Exception:
        rows = []
    for r in rows:
        cls = incidents.classify(r.get("incident_type", ""),
                                 db.loads(r.get("detail"), {}))
        if cls["attribution"] == "node":
            exec_b += _decay(age_d(r.get("occurred_at") or ""))

    # Chronic dropouts: nights reflow had to move this node's own remaining
    # plan items elsewhere, and the node delivered nothing that night either
    # (so the per-item accounting above never saw it). Escalates once the
    # node has racked up enough distinct dropout nights to look like a
    # recurring fault rather than one cloudy night.
    try:
        dropout_rows = db.query(
            "SELECT DISTINCT night, MAX(created_at) AS last_at FROM reflow_log "
            "WHERE from_node = %s AND created_at >= %s GROUP BY night",
            (node_id, since))
    except Exception:
        dropout_rows = []
    dropout_nights = [r for r in dropout_rows
                      if not delivered_by_night.get(r.get("night") or "")]
    if dropout_nights:
        quarantine = (DROPOUT_QUARANTINE_MULTIPLIER
                     if len(dropout_nights) >= DROPOUT_STREAK_THRESHOLD else 1.0)
        for r in dropout_nights:
            exec_b += (DROPOUT_FAIL_WEIGHT * quarantine
                      * _decay(age_d(r.get("last_at") or "")))

    # p_accept: delivered → accepted.
    acc_a, acc_b = PRIOR_A, PRIOR_B
    for m in meas:
        w = _decay(age_d(m.get("received_at") or ""))
        if _accepted(m):
            acc_a += w
        else:
            acc_b += w

    # kappa: delivered error vs the physics prediction, hardware-normalized.
    t_sub = float(node.get("max_exposure_s", 30.0) or 30.0)
    if str(node.get("mount_type") or "alt_az") == "alt_az":
        t_sub = min(t_sub, 30.0)   # field-rotation limit
    n_sub = 24                       # nominal 12-minute stack
    num, den, n_k = KAPPA_PRIOR_WEIGHT * 1.0, KAPPA_PRIOR_WEIGHT, 0
    for m in meas:
        if not _accepted(m):
            continue
        unc = m.get("uncertainty")
        mag = m.get("magnitude")
        am = m.get("airmass")
        if unc is None or mag is None or not unc:
            continue
        x = max(1.0, float(am)) if am else 1.2
        alt = math.degrees(math.asin(min(1.0, 1.0 / x)))
        pred = physics.sigma_total(node, float(mag), alt, t_sub, n_sub)
        ratio = min(100.0, max(0.1, (float(unc) / max(pred, 1e-4)) ** 2))
        w = _decay(age_d(m.get("received_at") or ""))
        num += w * ratio
        den += w
        n_k += 1
    kappa = round(min(KAPPA_BOUNDS[1], max(KAPPA_BOUNDS[0], num / den)), 4)

    p_exec, sd_e = _beta_stats(exec_a, exec_b)
    p_acc, sd_a = _beta_stats(acc_a, acc_b)
    detail = {
        "n_measurements": len(meas), "n_planned_nights": len(seen_nights),
        "p_exec": round(p_exec, 4), "p_accept": round(p_acc, 4),
        "sd_exec": round(sd_e, 4), "sd_accept": round(sd_a, 4),
        "dropout_nights": len(dropout_nights),
        "dropout_quarantined": len(dropout_nights) >= DROPOUT_STREAK_THRESHOLD,
    }
    db.execute(
        """INSERT INTO chorus_node_ledger
               (node_id, p_exec_a, p_exec_b, p_accept_a, p_accept_b,
                kappa, n_kappa, detail, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(node_id) DO UPDATE SET
               p_exec_a=excluded.p_exec_a, p_exec_b=excluded.p_exec_b,
               p_accept_a=excluded.p_accept_a, p_accept_b=excluded.p_accept_b,
               kappa=excluded.kappa, n_kappa=excluded.n_kappa,
               detail=excluded.detail, updated_at=excluded.updated_at""",
        (node_id, round(exec_a, 4), round(exec_b, 4), round(acc_a, 4),
         round(acc_b, 4), kappa, n_k, json.dumps(detail), _now()))

    # Monthly clear-sky climatology: EMA of "did this site produce data".
    _update_climatology(node_id, delivered_by_night, seen_nights)
    logger.info("Ledger %s: p_exec=%.3f p_accept=%.3f kappa=%.2f (n=%d)",
                node_id, p_exec, p_acc, kappa, len(meas))
    return detail


def _update_climatology(node_id: str, delivered_by_night: dict,
                        planned_nights: set) -> None:
    if not planned_nights:
        return
    row = db.query_one(
        "SELECT climatology FROM chorus_site_calibration WHERE node_id = %s",
        (node_id,))
    clim = db.loads(row.get("climatology") if row else None, {}) or {}
    for night in sorted(planned_nights):
        try:
            month = str(int(night[5:7]))
        except (ValueError, IndexError):
            continue
        realized = 1.0 if delivered_by_night.get(night) else 0.0
        old = float(clim.get(month, 0.5))
        clim[month] = round(0.9 * old + 0.1 * realized, 4)
    _upsert_calibration(node_id, climatology=clim)


def _upsert_calibration(node_id: str, a: Optional[float] = None,
                        b: Optional[float] = None,
                        n_nights: Optional[int] = None,
                        climatology: Optional[dict] = None) -> None:
    row = db.query_one(
        "SELECT * FROM chorus_site_calibration WHERE node_id = %s", (node_id,))
    cur_a = float(row["a"]) if row else 0.0
    cur_b = float(row["b"]) if row else 1.0
    cur_n = int(row["n_nights"]) if row else 0
    cur_c = db.loads(row.get("climatology") if row else None, {}) or {}
    db.execute(
        """INSERT INTO chorus_site_calibration
               (node_id, a, b, n_nights, climatology, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT(node_id) DO UPDATE SET
               a=excluded.a, b=excluded.b, n_nights=excluded.n_nights,
               climatology=excluded.climatology, updated_at=excluded.updated_at""",
        (node_id,
         a if a is not None else cur_a,
         b if b is not None else cur_b,
         n_nights if n_nights is not None else cur_n,
         json.dumps(climatology if climatology is not None else cur_c),
         _now()))


def site_calibrations() -> dict:
    """node_id → {a, b} for sites with enough history; identity elsewhere."""
    try:
        rows = db.query("SELECT node_id, a, b, n_nights FROM chorus_site_calibration")
    except Exception:
        return {}
    return {r["node_id"]: {"a": float(r["a"]), "b": float(r["b"])}
            for r in rows if int(r.get("n_nights", 0) or 0) >= 8}


def climatology_fn() -> Callable:
    """clear_prob(node, when) for the horizon sweep — monthly EMA per site,
    0.5 prior when unknown."""
    try:
        rows = db.query("SELECT node_id, climatology FROM chorus_site_calibration")
        clim = {r["node_id"]: (db.loads(r.get("climatology"), {}) or {})
                for r in rows}
    except Exception:
        clim = {}

    def clear_prob(node: dict, when: datetime) -> float:
        c = clim.get(node.get("node_id", ""), {})
        return float(c.get(str(when.month), 0.5))
    return clear_prob


# ── Site forecast calibration (Ring 0) ────────────────────────────────────────

def calibrate_sites(config: dict) -> int:
    """Fit p_realized ≈ logistic(a + b·logit(forecast_clear)) per site from
    archived forecast-vs-realized pairs.  Deterministic coarse grid search on
    log loss — no numerics, no LLM.  Returns the number of sites calibrated."""
    try:
        rows = db.query(
            """SELECT night, inputs, realized FROM chorus_run_archive
               WHERE realized IS NOT NULL AND realized != '' AND shadow = 0
               ORDER BY night DESC LIMIT 45""")
    except Exception:
        return 0

    pairs_by_node: dict = {}
    for r in rows:
        inputs = db.loads(r.get("inputs"), {})
        realized = db.loads(r.get("realized"), {})
        for nid, forecast in (inputs.get("forecast_by_node") or {}).items():
            real = realized.get(nid) or {}
            outcomes = [1.0 if (v or {}).get("delivered") else 0.0
                        for v in real.values()]
            if forecast is None or not outcomes:
                continue
            pairs_by_node.setdefault(nid, []).append(
                (float(forecast), sum(outcomes) / len(outcomes)))

    count = 0
    for nid, pairs in pairs_by_node.items():
        if len(pairs) < 8:
            continue
        best = (0.0, 1.0, float("inf"))
        for ai in range(-8, 9):
            a = ai * 0.25
            for bi in range(1, 13):
                b = bi * 0.25
                loss = 0.0
                for f, y in pairs:
                    f = min(max(f, 1e-3), 1.0 - 1e-3)
                    z = a + b * math.log(f / (1.0 - f))
                    p = 1.0 / (1.0 + math.exp(-z))
                    p = min(max(p, 1e-4), 1.0 - 1e-4)
                    loss -= y * math.log(p) + (1.0 - y) * math.log(1.0 - p)
                if loss < best[2] - 1e-9:
                    best = (a, b, loss)
        _upsert_calibration(nid, a=best[0], b=best[1], n_nights=len(pairs))
        count += 1
    if count:
        logger.info("Weather calibration updated for %d site(s)", count)
    return count


# ── Target science state ──────────────────────────────────────────────────────

def target_states() -> dict:
    try:
        rows = db.query("SELECT target_id, state FROM chorus_target_state")
    except Exception:
        return {}
    return {r["target_id"]: (db.loads(r.get("state"), {}) or {}) for r in rows}


def _write_state(target_id: str, state: dict) -> None:
    db.execute(
        """INSERT INTO chorus_target_state (target_id, state, updated_at)
           VALUES (%s,%s,%s)
           ON CONFLICT(target_id) DO UPDATE SET
               state=excluded.state, updated_at=excluded.updated_at""",
        (target_id, json.dumps(state), _now()))


def update_target_states(config: dict) -> int:
    """Nightly per-target state refresh: last accepted sample, CV outburst
    flag, EB/exoplanet ephemerides (from transit_ephemerides where known),
    and the persistent phase-coverage residual vector."""
    targets = db.query("SELECT * FROM targets WHERE active = 1")
    if not targets:
        return 0
    states = target_states()
    now = datetime.now(timezone.utc)
    ephem = {r["target_id"]: r for r in db.query(
        "SELECT target_id, period_days, epoch_bjd FROM transit_ephemerides")}

    count = 0
    for t in targets:
        tid = t["target_id"]
        state = states.get(tid, {})
        rows = db.query(
            """SELECT magnitude, uncertainty, received_at, aavso_submitted,
                      validation_status, quality_flag
               FROM measurements WHERE target_name = %s
               ORDER BY received_at DESC LIMIT 60""", (t["name"],))
        accepted = [m for m in rows if _accepted(m)]

        if accepted:
            state["last_accepted_utc"] = accepted[0]["received_at"]
            latest_mag = float(accepted[0]["magnitude"])
            cat = t.get("mag")
            if cat is not None:
                state["outburst"] = bool(latest_mag < float(cat) - OUTBURST_DELTA_MAG)

        if tid in ephem and not (state.get("ephemeris") or {}).get("period_days"):
            e = ephem[tid]
            state["ephemeris"] = {"period_days": float(e["period_days"]),
                                  "epoch_jd": float(e["epoch_bjd"])}

        # Phase-coverage ledger: seasonal decay toward 1, then draw down the
        # bins actually captured by accepted measurements.
        eph = state.get("ephemeris") or {}
        if eph.get("period_days") and cellmod.family_of(
                t.get("target_type", "")) == "eb":
            cov = state.get("phase_coverage") or [1.0] * cellmod.PHASE_BINS
            cov = [min(1.0, max(0.0, float(c))) for c in cov][:cellmod.PHASE_BINS]
            cov += [1.0] * (cellmod.PHASE_BINS - len(cov))
            last = state.get("phase_updated_at")
            if last:
                try:
                    dt_d = (now - datetime.fromisoformat(last)).total_seconds() / 86400.0
                    refresh = 0.5 ** (max(0.0, dt_d) / SEASON_HALF_LIFE_D)
                    cov = [1.0 - (1.0 - c) * refresh for c in cov]
                except ValueError:
                    pass
            for m in accepted:
                try:
                    when = datetime.fromisoformat(m["received_at"])
                except (TypeError, ValueError):
                    continue
                p = cellmod.phase_of(when, eph)
                if p is None:
                    continue
                b = min(cellmod.PHASE_BINS - 1, int(p * cellmod.PHASE_BINS))
                g = physics.capture(float(m.get("uncertainty") or 0.1), 0.03)
                cov[b] *= (1.0 - g)
            state["phase_coverage"] = [round(c, 4) for c in cov]
            state["phase_updated_at"] = _now()

        _write_state(tid, state)
        count += 1
    return count


# ── Realization join for archived runs (feeds backtest + calibration) ────────

def mark_realizations(config: dict) -> int:
    """Fill in the `realized` column of past archived runs: which planned
    (node, target) pairs actually delivered accepted data, and at what σ."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = db.query(
            """SELECT run_id, night, inputs FROM chorus_run_archive
               WHERE (realized IS NULL OR realized = '') AND night < %s
               ORDER BY night DESC LIMIT 10""", (today,))
    except Exception:
        return 0

    count = 0
    for r in rows:
        inputs = db.loads(r.get("inputs"), {})
        night = r["night"]
        try:
            night_end = (datetime.fromisoformat(night)
                         + timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            continue
        realized: dict = {}
        name_by_tid = inputs.get("target_names") or {}
        for nid in (inputs.get("forecast_by_node") or {}):
            per_target: dict = {}
            for tid, tname in name_by_tid.items():
                meas = db.query(
                    """SELECT uncertainty, aavso_submitted, validation_status,
                              quality_flag
                       FROM measurements
                       WHERE node_id = %s AND target_name = %s
                         AND received_at >= %s AND received_at < %s
                       LIMIT 20""",
                    (nid, tname, night + "T00:00:00",
                     night_end + "T00:00:00"))
                if not meas:
                    continue
                acc = [m for m in meas if _accepted(m)]
                sigmas = [float(m["uncertainty"]) for m in acc
                          if m.get("uncertainty")]
                per_target[tid] = {
                    "delivered": True,
                    "accepted": bool(acc),
                    "sigma": round(min(sigmas), 4) if sigmas else None,
                }
            realized[nid] = per_target
        db.execute(
            "UPDATE chorus_run_archive SET realized = %s WHERE run_id = %s",
            (json.dumps(realized), r["run_id"]))
        count += 1
    return count


# ── Nightly entry point ───────────────────────────────────────────────────────

def run_nightly(config: dict) -> None:
    """T0 + Ring 0, called from the maintenance loop.  Every stage is
    best-effort: a failure updates nothing and never blocks the others."""
    from cloud import registry
    for node in registry.list_nodes():
        if node.get("status") == "disabled":
            continue
        try:
            refresh_node(node)
        except Exception as exc:
            logger.error("Ledger refresh failed for %s: %s",
                         node.get("node_id"), exc)
    try:
        update_target_states(config)
    except Exception as exc:
        logger.error("Target state update failed: %s", exc)
    try:
        mark_realizations(config)
    except Exception as exc:
        logger.error("Realization join failed: %s", exc)
    try:
        calibrate_sites(config)
    except Exception as exc:
        logger.error("Site calibration failed: %s", exc)
