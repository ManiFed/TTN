#!/usr/bin/env python3
"""Network-wide, versioned photometric self-calibration.

The node's frame calibration remains immutable.  This module learns a second,
cloud-side correction from stable stars observed across overlapping nodes and
applies it only when a model has passed the configured shadow gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import psycopg2.extras

from cloud import db
from src.shared_models import PlanItem

logger = logging.getLogger("cloud.calibration")

BAD_FLAGS = {"variable", "variable_suspect", "candidate", "saturated",
             "blended", "trailed", "edge", "extended", "unmatched"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg(config: dict) -> dict:
    c = config.get("calibration_mesh") or {}
    return {
        "enabled": bool(c.get("enabled", True)),
        "apply_qualified": bool(c.get("apply_qualified", False)),
        "max_frame_zp_scatter": float(c.get("max_frame_zp_scatter", 0.15)),
        "min_samples": int(c.get("min_samples", 500)),
        "min_stars": int(c.get("min_stars", 50)),
        "min_nights": int(c.get("min_nights", 5)),
        "min_overlap_nodes": int(c.get("min_overlap_nodes", 3)),
        "min_color_span": float(c.get("min_color_span", 1.0)),
        "min_airmass_span": float(c.get("min_airmass_span", 0.8)),
        "standard_rms": float(c.get("standard_rms", 0.03)),
        "cv_rms": float(c.get("cv_rms", 0.05)),
        "min_improvement": float(c.get("min_improvement", 0.10)),
        "max_color_slope": float(c.get("max_color_slope", 0.02)),
        "passes_to_qualify": int(c.get("passes_to_qualify", 3)),
    }


def ingest_frame(node_id: str, frame: dict, sources: list, config: dict) -> int:
    """Store eligible stable-star samples from one already-ingested survey frame."""
    cfg = _cfg(config)
    if not cfg["enabled"]:
        return 0
    fingerprint = str(frame.get("response_fingerprint") or "").strip()
    family = str(frame.get("response_family") or "").strip()
    frame_id = str(frame.get("fits_file") or "").strip()
    if not fingerprint or not frame_id or frame.get("timestamp_trusted") is False:
        return 0
    db.execute(
        "INSERT INTO response_fingerprints(response_fingerprint,response_family,node_id,"
        "descriptor,first_seen_at,last_seen_at) VALUES(%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT(response_fingerprint) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at,"
        "descriptor=EXCLUDED.descriptor",
        (fingerprint, family, node_id, json.dumps(frame.get("response_descriptor") or {}),
         _now(), _now()))
    try:
        bjd = float(frame["bjd"])
        zp = float(frame.get("zero_point") or 0.0)
        zp_scatter = float(frame.get("zp_scatter") or 99.0)
    except (KeyError, TypeError, ValueError):
        return 0
    if zp_scatter > cfg["max_frame_zp_scatter"]:
        return 0
    try:
        airmass = float(frame["airmass"]) if frame.get("airmass") is not None else None
    except (TypeError, ValueError):
        airmass = None
    filt = str(frame.get("filter") or "CV")[:8]
    rows = []
    for s in sources:
        flags = {str(v).lower() for v in (s.get("flags") or [])}
        if flags & BAD_FLAGS or not s.get("matched"):
            continue
        try:
            inst = float(s["instrumental_mag"])
            inst_err = float(s.get("instrumental_err") or 0.05)
            cat = float(s["cat_mag"])
            cat_err = float(s.get("cat_err") or 0.05)
        except (KeyError, TypeError, ValueError):
            continue
        color = s.get("catalog_color")
        try:
            color = float(color) if color is not None else None
        except (TypeError, ValueError):
            color = None
        if not (0 < inst_err < 1 and 0 < cat_err < 1):
            continue
        rows.append((fingerprint, family, node_id, frame_id, str(s["key"]), bjd,
                     filt, inst, inst_err, zp, cat, cat_err,
                     str(s.get("catalog_band") or ""), color, airmass,
                     json.dumps(sorted(flags)), _now()))
    if not rows:
        return 0
    conn = db.connect()
    try:
        with conn:
            cur = conn.cursor()
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO calibration_samples "
                "(response_fingerprint,response_family,node_id,frame_id,source_key,bjd,filter,"
                " instrumental_mag,instrumental_err,frame_zero_point,catalog_mag,"
                " catalog_err,catalog_band,catalog_color,airmass,flags,created_at) VALUES %s "
                "ON CONFLICT(response_fingerprint,frame_id,source_key) DO NOTHING",
                rows,
            )
    finally:
        db.release(conn)
    return len(rows)


def _design(rows: list, pivot_color: float, pivot_bjd: float) -> tuple:
    x, y, sigma = [], [], []
    for r in rows:
        color = r.get("catalog_color")
        airmass = r.get("airmass")
        x.append([1.0,
                  (float(color) - pivot_color) if color is not None else 0.0,
                  (float(airmass) - 1.0) if airmass is not None else 0.0,
                  float(r["bjd"]) - pivot_bjd])
        observed = float(r["instrumental_mag"]) + float(r["frame_zero_point"])
        y.append(observed - float(r["catalog_mag"]))
        sigma.append(math.sqrt(float(r["instrumental_err"]) ** 2
                               + float(r["catalog_err"]) ** 2))
    return np.asarray(x), np.asarray(y), np.maximum(np.asarray(sigma), 0.005)


def fit_samples(rows: list, filter_name: str = "CV") -> Optional[dict]:
    """Pure robust fit used by the nightly worker and synthetic tests."""
    if len(rows) < 8:
        return None
    colors = [float(r["catalog_color"]) for r in rows
              if r.get("catalog_color") is not None]
    pivot_color = float(np.median(colors)) if colors else 0.0
    pivot_bjd = float(np.median([float(r["bjd"]) for r in rows]))

    # Deterministic source-level holdout prevents the same star leaking across
    # training and validation exposures.
    train = [r for r in rows if int(hashlib.sha256(
        str(r["source_key"]).encode()).hexdigest()[:4], 16) % 5 != 0]
    hold = [r for r in rows if r not in train]
    if len(train) < 6 or len(hold) < 2:
        train, hold = rows[:-max(2, len(rows)//5)], rows[-max(2, len(rows)//5):]

    x, y, sigma = _design(train, pivot_color, pivot_bjd)
    base_w = 1.0 / np.square(sigma)
    beta = np.zeros(x.shape[1])
    for _ in range(8):
        pred = x @ beta
        resid = y - pred
        scale = max(float(np.median(np.abs(resid - np.median(resid))) * 1.4826), 0.005)
        huber = np.ones_like(resid)
        mask = np.abs(resid) > 1.5 * scale
        huber[mask] = (1.5 * scale) / np.abs(resid[mask])
        w = base_w * huber
        beta = np.linalg.lstsq(x * np.sqrt(w[:, None]), y * np.sqrt(w), rcond=None)[0]

    # Extinction varies night-to-night. Fit a per-night nuisance coefficient
    # after the stable node/color/drift terms; the stored scalar is the median
    # fallback for a night without enough leverage.
    nightly_extinction = {}
    for night in sorted({int(float(r["bjd"])) for r in train}):
        subset = [r for r in train if int(float(r["bjd"])) == night
                  and r.get("airmass") is not None]
        if len(subset) < 3:
            continue
        sx, sy, ss = _design(subset, pivot_color, pivot_bjd)
        a = sx[:, 2]
        base = sy - (sx[:, 0] * beta[0] + sx[:, 1] * beta[1] + sx[:, 3] * beta[3])
        w = 1.0 / np.square(ss)
        denom = float(np.sum(w * a * a))
        if denom > 0:
            nightly_extinction[str(night)] = float(np.sum(w * a * base) / denom)
    if nightly_extinction:
        beta[2] = float(np.median(list(nightly_extinction.values())))

    hx, hy, hs = _design(hold, pivot_color, pivot_bjd)
    prediction = hx @ beta
    for i, row in enumerate(hold):
        nightly = nightly_extinction.get(str(int(float(row["bjd"]))))
        if nightly is not None:
            prediction[i] += (nightly - beta[2]) * hx[i, 2]
    residual = hy - prediction
    weighted_rms = float(math.sqrt(np.average(np.square(residual), weights=1/np.square(hs))))
    baseline = hy
    baseline_rms = float(math.sqrt(np.average(np.square(baseline), weights=1/np.square(hs))))
    improvement = 1.0 - weighted_rms / max(baseline_rms, 1e-9)
    color_resid = []
    for r, e in zip(hold, residual):
        if r.get("catalog_color") is not None:
            color_resid.append((float(r["catalog_color"]), float(e)))
    color_slope = 0.0
    if len(color_resid) >= 3 and np.ptp([p[0] for p in color_resid]) > 0:
        color_slope = float(np.polyfit([p[0] for p in color_resid],
                                       [p[1] for p in color_resid], 1)[0])
    normalized_filter = str(filter_name or "CV").upper()
    bands = [str(r.get("catalog_band") or "").upper() for r in rows]
    band_match_fraction = (sum(b == normalized_filter for b in bands) / len(bands)
                           if bands and normalized_filter != "CV" else 1.0)
    return {
        "offset": float(beta[0]), "color_term": float(beta[1]),
        "extinction": float(beta[2]), "drift_per_day": float(beta[3]),
        "pivot_color": pivot_color, "pivot_bjd": pivot_bjd,
        "model_uncertainty": weighted_rms,
        "weighted_rms": weighted_rms, "baseline_rms": baseline_rms,
        "improvement": improvement, "color_slope": color_slope,
        "n_samples": len(rows),
        "n_stars": len({r["source_key"] for r in rows}),
        "n_nights": len({int(float(r["bjd"])) for r in rows}),
        "color_span": float(np.ptp(colors)) if colors else 0.0,
        "airmass_span": float(np.ptp([float(r["airmass"]) for r in rows
                                       if r.get("airmass") is not None]))
                        if any(r.get("airmass") is not None for r in rows) else 0.0,
        "filter": filter_name,
        "catalog_bands": sorted(set(b for b in bands if b)),
        "band_match_fraction": band_match_fraction,
        "nightly_extinction": nightly_extinction,
    }


def _passes_gate(stats: dict, overlap_nodes: int, cfg: dict) -> bool:
    rms_limit = cfg["cv_rms"] if stats["filter"].upper() == "CV" else cfg["standard_rms"]
    return all((
        stats["n_samples"] >= cfg["min_samples"],
        stats["n_stars"] >= cfg["min_stars"],
        stats["n_nights"] >= cfg["min_nights"],
        overlap_nodes >= cfg["min_overlap_nodes"],
        stats["color_span"] >= cfg["min_color_span"],
        stats["airmass_span"] >= cfg["min_airmass_span"],
        stats["weighted_rms"] <= rms_limit,
        stats["improvement"] >= cfg["min_improvement"],
        abs(stats["color_slope"]) <= cfg["max_color_slope"],
        # Standard-band output requires reference photometry in that same
        # standard band. A V-only catalog may improve CV in shadow, but it can
        # never qualify a B/R/I response or relabel a natural-system result.
        stats.get("band_match_fraction", 0.0) >= 0.9,
    ))


def run_nightly(config: dict) -> dict:
    """Fit and version every response/filter represented in the sample store."""
    cfg = _cfg(config)
    if not cfg["enabled"]:
        return {"models": 0, "qualified": 0}
    groups = db.query(
        "SELECT DISTINCT response_fingerprint,response_family,node_id,filter FROM calibration_samples")
    made = qualified = 0
    for group in groups:
        rows = db.query(
            "SELECT * FROM calibration_samples WHERE response_fingerprint=%s AND filter=%s ORDER BY bjd",
            (group["response_fingerprint"], group["filter"]))
        stats = fit_samples(rows, group["filter"])
        if not stats:
            continue
        prior_models = db.query(
            "SELECT \"offset\",color_term FROM photometric_models WHERE response_family=%s "
            "AND filter=%s AND state='qualified' AND response_fingerprint<>%s",
            (group.get("response_family") or "", group["filter"],
             group["response_fingerprint"])) if group.get("response_family") else []
        if prior_models and stats["n_samples"] < cfg["min_samples"]:
            strength = float((config.get("calibration_mesh") or {}).get(
                "family_prior_samples", 100))
            weight = stats["n_samples"] / max(stats["n_samples"] + strength, 1)
            stats["offset"] = (weight * stats["offset"] + (1 - weight)
                               * float(np.mean([m["offset"] for m in prior_models])))
            stats["color_term"] = (weight * stats["color_term"] + (1 - weight)
                                   * float(np.mean([m["color_term"] for m in prior_models])))
            stats["family_prior"] = {"models": len(prior_models), "data_weight": weight}
        source_keys = list({r["source_key"] for r in rows})
        overlap = db.query_one(
            "SELECT COUNT(DISTINCT node_id) AS n FROM calibration_samples WHERE source_key=ANY(%s) AND filter=%s",
            (source_keys, group["filter"])) or {"n": 0}
        overlap_nodes = int(overlap.get("n") or 0)
        gate = _passes_gate(stats, overlap_nodes, cfg)
        prev = db.query_one(
            "SELECT * FROM photometric_models WHERE response_fingerprint=%s AND filter=%s "
            "ORDER BY created_at DESC LIMIT 1", (group["response_fingerprint"], group["filter"]))
        passes = (int(prev.get("consecutive_passes") or 0) + 1) if gate and prev else (1 if gate else 0)
        if gate and passes >= cfg["passes_to_qualify"]:
            state = "qualified"
        elif prev and prev.get("state") == "qualified" and not gate:
            state = "degraded"
        else:
            state = "shadow"
        if state == "qualified":
            qualified += 1
        version = "cal_" + uuid.uuid4().hex[:16]
        validation = {**stats, "overlap_nodes": overlap_nodes, "gate_pass": gate}
        db.execute(
            "INSERT INTO photometric_models "
            "(model_version,response_fingerprint,response_family,node_id,filter,state,\"offset\",color_term,"
            " extinction,drift_per_day,pivot_color,model_uncertainty,validation,"
            " consecutive_passes,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (version, group["response_fingerprint"], group.get("response_family") or "",
             group["node_id"], group["filter"],
             state, stats["offset"], stats["color_term"], stats["extinction"],
             stats["drift_per_day"], stats["pivot_color"], stats["model_uncertainty"],
             json.dumps(validation), passes, _now()))
        made += 1
    return {"models": made, "qualified": qualified}


def latest_model(fingerprint: str, filter_name: str,
                 qualified_only: bool = False) -> Optional[dict]:
    row = db.query_one(
        "SELECT * FROM photometric_models WHERE response_fingerprint=%s AND filter=%s "
        "ORDER BY created_at DESC LIMIT 1",
        (fingerprint, filter_name))
    if not row or row.get("state") not in ("qualified", "shadow"):
        return None
    if qualified_only and row.get("state") != "qualified":
        return None
    row["validation"] = db.loads(row.get("validation"), {})
    return row


def apply_to_measurement(measurement_id: int, payload: dict, config: dict) -> Optional[dict]:
    fingerprint = str(payload.get("response_fingerprint") or "")
    filt = str(payload.get("filter") or "CV")
    if not fingerprint:
        return None
    cfg = _cfg(config)
    model = latest_model(fingerprint, filt, qualified_only=False)
    if not model:
        return None
    raw = float(payload["magnitude"])
    airmass = float(payload.get("airmass") or 1.0)
    color = payload.get("target_catalog_color")
    color_delta = (float(color) - float(model["pivot_color"])) if color is not None else 0.0
    bjd = float(payload.get("bjd") or 0.0)
    pivot_bjd = float((model.get("validation") or {}).get("pivot_bjd") or bjd)
    nightly = (model.get("validation") or {}).get("nightly_extinction") or {}
    extinction = float(nightly.get(str(int(bjd)), model["extinction"]))
    residual = (float(model["offset"])
                + float(model["color_term"]) * color_delta
                + extinction * (airmass - 1.0)
                + float(model.get("drift_per_day") or 0.0) * (bjd - pivot_bjd))
    correction = -residual
    network_mag = raw + correction
    network_unc = math.sqrt(float(payload.get("uncertainty") or 0.0) ** 2
                            + float(model["model_uncertainty"]) ** 2)
    applied = model["state"] == "qualified" and cfg["apply_qualified"]
    db.execute(
        "UPDATE measurements SET network_magnitude=%s, network_uncertainty=%s,"
        " calibration_correction=%s, calibration_model_version=%s,"
        " calibration_state=%s, magnitude_system=%s WHERE id=%s",
        (network_mag, network_unc, correction, model["model_version"],
         "applied" if applied else model["state"],
         filt if filt.upper() != "CV" else f"TN-CV/{fingerprint}", measurement_id))
    return {"network_magnitude": network_mag, "network_uncertainty": network_unc,
            "model_version": model["model_version"], "applied": applied}


def rollback(model_version: str) -> bool:
    row = db.query_one("SELECT * FROM photometric_models WHERE model_version=%s", (model_version,))
    if not row:
        return False
    db.execute("UPDATE photometric_models SET state='degraded', retired_at=%s WHERE model_version=%s",
               (_now(), model_version))
    db.execute("UPDATE measurements SET calibration_state='degraded' WHERE calibration_model_version=%s",
               (model_version,))
    return True


def calibration_debt_item(ctx, node: dict, items: list, config: dict) -> Optional[PlanItem]:
    """Return one low-value standard-field visit only after passive data stalls."""
    raw = config.get("calibration_mesh") or {}
    fields = list(raw.get("standard_fields") or [])
    if not raw.get("calibration_debt_enabled", False) or not fields:
        return None
    passive_nights = int(raw.get("debt_after_photometric_nights", 30))
    summary = db.query_one(
        "SELECT COUNT(DISTINCT FLOOR(bjd)) AS nights,COUNT(*) AS samples "
        "FROM calibration_samples WHERE node_id=%s", (ctx.node_id,)) or {}
    if int(summary.get("nights") or 0) < passive_nights:
        return None
    qualified = db.query_one(
        "SELECT 1 FROM photometric_models WHERE node_id=%s AND state='qualified' LIMIT 1",
        (ctx.node_id,))
    if qualified:
        return None
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    weekly = db.query_one(
        "SELECT COUNT(*) AS n FROM calibration_opportunities WHERE node_id=%s "
        "AND scheduled_at>=%s", (ctx.node_id, week_ago)) or {}
    if int(weekly.get("n") or 0) >= int(raw.get("max_fields_per_week", 2)):
        return None
    dark_minutes = max(1.0, (ctx.t1 - ctx.t0).total_seconds() / 60.0)
    science_minutes = sum(float(i.expDur) * int(i.expCount) / 60.0 for i in items)
    if science_minutes >= dark_minutes * 0.95:
        return None

    from cloud.conditions import target_alt
    used = []
    for item in items:
        try:
            used.append(datetime.fromisoformat(item.starts_at_utc.replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            pass
    candidates = []
    for field in fields:
        try:
            ra, dec = float(field["ra_deg"]), float(field["dec_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        for slot in range(ctx.n_slots):
            when = ctx.slot_utc(slot)
            if any(abs((when - other).total_seconds()) < 20 * 60 for other in used):
                continue
            alt = target_alt(ra, dec, ctx.lat, ctx.lon, when)
            if alt >= ctx.min_alt:
                # Prefer requested missing leverage, otherwise transit altitude.
                wants_airmass = "airmass" in (field.get("leverage") or [])
                rank = alt if not wants_airmass else -alt
                candidates.append((-float(field.get("color_span") or 0), -rank,
                                   str(field.get("name") or "standard field"), when, field))
    if not candidates:
        return None
    _, _, name, when, field = min(candidates)
    max_duration_s = dark_minutes * 60.0 * 0.05
    exp = min(float(field.get("expDur") or 20), float(node.get("max_exposure_s") or 30))
    count = min(int(field.get("expCount") or 12), max(1, int(max_duration_s / exp)))
    fingerprint_row = db.query_one(
        "SELECT response_fingerprint FROM calibration_samples WHERE node_id=%s "
        "ORDER BY created_at DESC LIMIT 1", (ctx.node_id,)) or {}
    fingerprint = str(fingerprint_row.get("response_fingerprint") or "")
    return PlanItem(
        target=name, target_id="calibration:" + name, ra=round(float(field["ra_deg"]) / 15, 6),
        dec=round(float(field["dec_deg"]), 6), expDur=exp, expCount=count,
        startTime=(when + ctx.utc_offset).strftime("%H:%M"),
        starts_at_utc=when.isoformat(), latest_start_utc=(when + timedelta(minutes=15)).isoformat(),
        filter=str(field.get("filter") or "CV"), task_type="calibration", priority=0.001,
        item_id="item_" + hashlib.sha256(
            f"{ctx.node_id}:{name}:{when.isoformat()}".encode()).hexdigest()[:16],
        notes="CHORUS calibration debt: missing color/airmass leverage",
        explanation={"engine": "chorus", "calibration_debt": True,
                     "response_fingerprint": fingerprint})


def record_calibration_debt(node_id: str, plan_id: str, item: PlanItem) -> None:
    db.execute(
        "INSERT INTO calibration_opportunities(node_id,response_fingerprint,field_name,plan_id,"
        "scheduled_at) VALUES(%s,%s,%s,%s,%s)",
        (node_id, str(item.explanation.get("response_fingerprint") or ""), item.target,
         plan_id, _now()))
