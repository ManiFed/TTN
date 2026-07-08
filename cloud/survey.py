"""
Open Aperture survey store — every star in every frame.

Nodes (and contributed frames) upload compact per-frame source lists to
POST /api/v1/survey. This module ingests them:

    1. bounds-check the batch
    2. insert raw rows into survey_measurements (deduped on
       (source_key, node_id, bjd) — only genuinely new rows count)
    3. fold each new measurement into the per-(star, filter) Welford
       aggregates in survey_sources (the permanent science product)
    4. run two-tier deviation detection against the pre-update baseline
       and open/advance discovery_candidates rows

Candidate lifecycle (crossmatch.py moves rows past 'crossmatching'):

    detected ─(2-detection gate)→ crossmatching → known_vsx | known_tns
                                                → candidate ─(human)→ confirmed | rejected
"""

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import psycopg2.extras

from cloud import db

logger = logging.getLogger("cloud.survey")

# States in which a candidate row is still "open" for this source.
OPEN_STATES = ("detected", "crossmatching", "candidate")

# Two nodes within this window corroborate one another (matches the
# cross-validation window used for AAVSO measurements).
COTEMPORAL_DAYS = 0.03      # ≈ 43 minutes
SAME_NIGHT_DAYS = 0.5

# Per-node per-night stored-deviant cap; in-memory is fine on the
# single-process cloud (gunicorn 1 worker).
_deviant_counts: dict = {}
_deviant_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg(config: dict, key: str, default):
    return (config.get("survey") or {}).get(key, default)


def _valid_source(s: dict) -> bool:
    try:
        return (
            bool(s.get("key"))
            and 0.0 <= float(s["ra"]) < 360.0
            and -90.0 <= float(s["dec"]) <= 90.0
            and -2.0 < float(s["mag"]) < 25.0
            and 0.0 < float(s.get("mag_err") or 0.05) < 2.0
        )
    except (KeyError, TypeError, ValueError):
        return False


def ingest_batch(node_id: str, payload: dict, config: dict,
                 node_tier: int = 1, historical: bool = False,
                 provenance: dict | None = None) -> dict:
    """Ingest one frame's survey source list. Returns a summary dict.

    node_tier 0 marks a contributor (unscheduled) source: its detections
    count, but can never satisfy the corroboration gate on their own.

    historical=True routes an archive frame (DATE-OBS in the past): baselines
    still absorb its non-deviant points (Welford is order-independent), but
    deviant points are EXCLUDED from the fold (so a 2023 nova can't inflate the
    mean it is later compared against) and written to retro_discoveries instead
    of the live candidate flow — no interrupts, no reflex.
    provenance {contribution_id, user_id} credits the human who caught it."""
    prov = provenance or {}
    frame = payload.get("frame") or {}
    sources = payload.get("sources") or []
    if not isinstance(sources, list) or not sources:
        return {"ok": False, "error": "no sources"}

    max_batch = int(_cfg(config, "max_sources_per_batch", 800))
    if len(sources) > max_batch:
        return {"ok": False, "error": f"batch exceeds {max_batch} sources"}

    try:
        bjd = float(frame.get("bjd"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "frame.bjd missing"}
    if not (2440000.0 < bjd < 2500000.0):
        return {"ok": False, "error": "frame.bjd out of range"}

    filter_name = str(frame.get("filter") or "CV")[:8]
    frame_id = str(frame.get("fits_file") or "")[:120]
    try:
        zp_scatter = float(frame.get("zp_scatter") or 0.05)
    except (TypeError, ValueError):
        zp_scatter = 0.05

    clean = [s for s in sources if _valid_source(s)]
    n_invalid = len(sources) - len(clean)
    if not clean:
        return {"ok": False, "error": "no valid sources"}
    # One frame = one bjd, so duplicate keys within the batch are the same
    # star detected twice (blends); keep the first (brightest, list is
    # peak-sorted on the node).
    seen: set = set()
    batch = []
    for s in clean:
        if s["key"] in seen:
            continue
        seen.add(s["key"])
        batch.append(s)

    now = _now()
    conn = db.connect()
    try:
        with conn:
            cur = conn.cursor()

            # Pre-update baselines: deviation detection compares each new
            # measurement against the network's knowledge *before* it.
            keys = [s["key"] for s in batch]
            cur.execute(
                "SELECT source_key, n_obs, mean_mag, m2, vsx_name, "
                "       variability_flag "
                "FROM survey_sources WHERE filter = %s AND source_key = ANY(%s)",
                (filter_name, keys),
            )
            baselines = {r[0]: {"n_obs": r[1], "mean_mag": r[2], "m2": r[3],
                                "vsx_name": r[4], "variability_flag": r[5]}
                         for r in cur.fetchall()}

            # Raw measurements — RETURNING tells us which rows are genuinely
            # new so retried/duplicate frames never double-count baselines.
            contribution_id = prov.get("contribution_id")
            user_id = str(prov.get("user_id") or "")
            inserted = psycopg2.extras.execute_values(
                cur,
                "INSERT INTO survey_measurements "
                "(source_key, node_id, bjd, mag, mag_err, snr, filter, "
                " frame_id, received_at, contribution_id, user_id) VALUES %s "
                "ON CONFLICT (source_key, node_id, bjd) DO NOTHING "
                "RETURNING source_key",
                [(s["key"], node_id, bjd, float(s["mag"]),
                  float(s.get("mag_err") or 0.05), float(s.get("snr") or 0.0),
                  filter_name, frame_id, now, contribution_id, user_id)
                 for s in batch],
                fetch=True,
            )
            new_keys = {r[0] for r in inserted}
            new_sources = [s for s in batch if s["key"] in new_keys]

            # Historical path: classify first so deviant archive points are
            # kept out of the baseline fold. deviations is [] for live frames.
            deviations = []
            if historical and new_sources:
                deviations = _classify_deviations(
                    bjd, filter_name, zp_scatter, new_sources, baselines, config)
            deviant_keys = {s["key"] for s, _, _ in deviations}
            fold_sources = [s for s in new_sources
                            if s["key"] not in deviant_keys]

            if fold_sources:
                # Historical frames must not regress a source's last-seen point,
                # so last_bjd/last_mag advance only when this frame is newer.
                if historical:
                    last_bjd_clause = (
                        "last_bjd = GREATEST("
                        " COALESCE(survey_sources.last_bjd, EXCLUDED.last_bjd),"
                        " EXCLUDED.last_bjd), ")
                    last_mag_clause = (
                        "last_mag = CASE WHEN EXCLUDED.last_bjd >= "
                        "   COALESCE(survey_sources.last_bjd, EXCLUDED.last_bjd) "
                        "   THEN EXCLUDED.last_mag ELSE survey_sources.last_mag END, ")
                else:
                    last_bjd_clause = "last_bjd = EXCLUDED.last_bjd, "
                    last_mag_clause = "last_mag = EXCLUDED.last_mag, "
                # Welford in SQL: mean' = mean + (x−mean)/n' ;
                # m2' = m2 + (x−mean)·(x−mean').  New rows start at n=1, m2=0.
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO survey_sources "
                    "(source_key, filter, ra_deg, dec_deg, catalog_mag, "
                    " catalog_err, catalog_src, n_obs, mean_mag, m2, "
                    " last_bjd, last_mag, updated_at) VALUES %s "
                    "ON CONFLICT (source_key, filter) DO UPDATE SET "
                    " n_obs    = survey_sources.n_obs + 1, "
                    " mean_mag = survey_sources.mean_mag "
                    "  + (EXCLUDED.mean_mag - survey_sources.mean_mag) "
                    "    / (survey_sources.n_obs + 1), "
                    " m2       = survey_sources.m2 "
                    "  + (EXCLUDED.mean_mag - survey_sources.mean_mag) "
                    "  * (EXCLUDED.mean_mag - (survey_sources.mean_mag "
                    "     + (EXCLUDED.mean_mag - survey_sources.mean_mag) "
                    "       / (survey_sources.n_obs + 1))), "
                    + last_bjd_clause + last_mag_clause +
                    " catalog_mag = COALESCE(survey_sources.catalog_mag, "
                    "                        EXCLUDED.catalog_mag), "
                    " catalog_err = COALESCE(survey_sources.catalog_err, "
                    "                        EXCLUDED.catalog_err), "
                    " catalog_src = CASE WHEN survey_sources.catalog_src = '' "
                    "   THEN EXCLUDED.catalog_src "
                    "   ELSE survey_sources.catalog_src END, "
                    " updated_at = EXCLUDED.updated_at",
                    [(s["key"], filter_name, float(s["ra"]), float(s["dec"]),
                      s.get("cat_mag"), s.get("cat_err"),
                      str(s.get("cat_src") or "")[:40],
                      1, float(s["mag"]), 0.0, bjd, float(s["mag"]), now)
                     for s in fold_sources],
                )

                # Stars whose own history scatters > threshold are variables,
                # not alerts: flag once so they stop opening candidates and
                # become survey science instead.
                suspect_stdev = float(_cfg(config, "variable_suspect_stdev", 0.2))
                cur.execute(
                    "UPDATE survey_sources SET variability_flag = 'variable_suspect' "
                    "WHERE filter = %s AND source_key = ANY(%s) "
                    "  AND variability_flag = 'none' AND vsx_name = '' "
                    "  AND n_obs >= 10 AND sqrt(m2 / (n_obs - 1)) > %s",
                    (filter_name, [s["key"] for s in fold_sources], suspect_stdev),
                )

        n_deviants = 0
        if historical:
            # Retrospective: never opens live candidates / interrupts / reflex.
            for s, kind, delta in deviations:
                if _record_retrospective(node_id, bjd, filter_name, s, kind,
                                         delta, config, prov):
                    n_deviants += 1
        elif new_sources:
            n_deviants = _detect_deviations(
                node_id, bjd, filter_name, zp_scatter,
                new_sources, baselines, config, node_tier)

        return {
            "ok": True,
            "accepted": len(new_sources),
            "duplicates": len(batch) - len(new_sources),
            "invalid": n_invalid,
            "deviants": n_deviants,
            "historical": historical,
        }
    finally:
        db.release(conn)


# ── Deviation detection ────────────────────────────────────────────────────────

def _classify_deviations(bjd: float, filter_name: str, zp_scatter: float,
                         sources: list, baselines: dict,
                         config: dict) -> list:
    """Two-tier deviation classification against pre-update baselines.

    Pure (no DB writes): returns [(source, kind, delta)] for every source that
    deviates. Shared by the live path (opens candidates) and the historical
    path (writes retro_discoveries + is excluded from the baseline fold).

    Bootstrap (n_obs < baseline_min_n): brightening vs. catalog only, with a
    deliberately high bar — catalog systematics ride in each source's cat_err.
    Baselined: both directions against the network's own running mean.
    """
    boot_delta = float(_cfg(config, "bootstrap_min_delta_mag", 1.0))
    boot_sigma = float(_cfg(config, "bootstrap_min_sigma", 5.0))
    base_min_n = int(_cfg(config, "baseline_min_n", 5))
    base_z     = float(_cfg(config, "baseline_min_z", 5.0))
    base_delta = float(_cfg(config, "baseline_min_delta_mag", 0.3))
    new_src_snr = float(_cfg(config, "new_source_min_snr", 10.0))

    out = []
    for s in sources:
        base = baselines.get(s["key"])
        if base and (base.get("vsx_name") or
                     base.get("variability_flag") == "variable_suspect"):
            continue

        mag = float(s["mag"])
        mag_err = float(s.get("mag_err") or 0.05)
        kind = None
        delta = None

        if not s.get("matched"):
            if float(s.get("snr") or 0.0) >= new_src_snr:
                kind, delta = "new_source", None
        elif base and (base.get("n_obs") or 0) >= base_min_n:
            mean = float(base["mean_mag"])
            n = int(base["n_obs"])
            var = float(base["m2"]) / max(n - 1, 1)
            sigma = math.sqrt(mag_err ** 2 + var)
            dev = mag - mean
            if abs(dev) >= base_delta and abs(dev) >= base_z * max(sigma, 1e-3):
                kind = "brightening" if dev < 0 else "fading"
                delta = abs(dev)
        else:
            cat_mag = s.get("cat_mag")
            if cat_mag is not None:
                cat_err = float(s.get("cat_err") or 0.05)
                sigma_tot = math.sqrt(mag_err ** 2 + cat_err ** 2 + zp_scatter ** 2)
                dev = float(cat_mag) - mag     # positive = brighter than catalog
                if dev >= boot_delta and dev >= boot_sigma * max(sigma_tot, 1e-3):
                    kind, delta = "brightening", dev

        if kind is not None:
            out.append((s, kind, delta))
    return out


def _detect_deviations(node_id: str, bjd: float, filter_name: str,
                       zp_scatter: float, sources: list, baselines: dict,
                       config: dict, node_tier: int = 1) -> int:
    """Live path: classify, then open/advance discovery candidates."""
    n_opened = 0
    for s, kind, delta in _classify_deviations(
            bjd, filter_name, zp_scatter, sources, baselines, config):
        if _record_detection(node_id, bjd, filter_name, s, kind, delta,
                             config, node_tier):
            n_opened += 1
    return n_opened


def _deviant_cap_reached(node_id: str, config: dict) -> bool:
    cap = int(_cfg(config, "deviants_per_node_night", 50))
    day = time.strftime("%Y%m%d", time.gmtime())
    with _deviant_lock:
        # Drop stale day counters so the dict never grows unbounded.
        for k in [k for k in _deviant_counts if k[1] != day]:
            del _deviant_counts[k]
        count = _deviant_counts.get((node_id, day), 0)
        if count >= cap:
            return True
        _deviant_counts[(node_id, day)] = count + 1
        return False


def _record_detection(node_id: str, bjd: float, filter_name: str, s: dict,
                      kind: str, delta: Optional[float], config: dict,
                      node_tier: int = 1) -> bool:
    """Open or advance a discovery candidate. Returns True when a row was
    created or promoted past the 2-detection gate."""
    ra, dec = float(s["ra"]), float(s["dec"])

    open_cand = db.query_one(
        "SELECT * FROM discovery_candidates "
        "WHERE source_key = %s AND state IN %s "
        "ORDER BY id DESC LIMIT 1",
        (s["key"], OPEN_STATES),
    )
    if open_cand is None and kind == "new_source":
        # Positional keys can split across their 3.6″ quantization bins, so
        # new-source detections also cone-match nearby open candidates.
        thr = 6.0 / 3600.0
        cos_dec = max(math.cos(math.radians(dec)), 0.05)
        open_cand = db.query_one(
            "SELECT * FROM discovery_candidates "
            "WHERE kind = 'new_source' AND state IN %s "
            "  AND dec_deg BETWEEN %s AND %s AND ra_deg BETWEEN %s AND %s "
            "ORDER BY id DESC LIMIT 1",
            (OPEN_STATES, dec - thr, dec + thr,
             ra - thr / cos_dec, ra + thr / cos_dec),
        )

    now = _now()
    if open_cand is not None:
        node_ids = db.loads(open_cand.get("node_ids"), [])
        if node_id not in node_ids:
            node_ids.append(node_id)
        detail = db.loads(open_cand.get("detail"), {})
        if node_tier > 0:
            detail["scheduled_seen"] = True
        first_bjd = float(open_cand.get("first_bjd") or bjd)
        n_det = int(open_cand.get("n_detections") or 1) + 1
        gap = abs(bjd - first_bjd)
        # Corroboration: a lone contributor can never promote a candidate —
        # it takes a scheduled node's detection or a second independent
        # source (data-poisoning guard for the open contribution pipeline).
        corroborated = bool(detail.get("scheduled_seen")) or len(node_ids) >= 2
        promoted = (
            open_cand["state"] == "detected"
            and corroborated
            and ((len(node_ids) >= 2 and gap <= COTEMPORAL_DAYS + 1.0)
                 or gap <= SAME_NIGHT_DAYS)
            and n_det >= 2
        )
        peak = max(float(open_cand.get("peak_delta_mag") or 0.0), delta or 0.0)
        db.execute(
            "UPDATE discovery_candidates SET "
            " n_detections = %s, n_nodes = %s, node_ids = %s, last_bjd = %s, "
            " last_mag = %s, peak_delta_mag = %s, state = %s, detail = %s, "
            " updated_at = %s "
            "WHERE id = %s",
            (n_det, len(node_ids), json.dumps(node_ids), bjd,
             float(s["mag"]), peak,
             "crossmatching" if promoted else open_cand["state"],
             json.dumps(detail), now, open_cand["id"]),
        )
        if promoted:
            logger.info("Discovery candidate #%s promoted to crossmatching: "
                        "%s %s Δ=%.2f (%d detections, %d nodes)",
                        open_cand["id"], kind, s["key"], peak, n_det,
                        len(node_ids))
            # THE ORGANISM: reflex-confirm this candidate on other dark nodes
            # now, instead of waiting for the nightly replan. Fully guarded and
            # best-effort — a reflex failure never affects the survey ingest.
            try:
                from cloud import reflex
                reflex.on_candidate_promoted({
                    "source_key": s["key"], "ra_deg": ra, "dec_deg": dec,
                    "kind": kind, "peak_delta_mag": peak,
                    "last_mag": float(s["mag"]),
                    "target_id": open_cand.get("target_id") or None,
                }, config)
            except Exception as exc:
                logger.debug("reflex hook failed for %s: %s", s["key"], exc)
        return promoted

    if _deviant_cap_reached(node_id, config):
        return False
    db.execute(
        "INSERT INTO discovery_candidates "
        "(source_key, ra_deg, dec_deg, kind, filter, first_bjd, last_bjd, "
        " n_detections, n_nodes, node_ids, peak_delta_mag, last_mag, state, "
        " detail, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 1, %s, %s, %s, 'detected', "
        "        %s, %s, %s)",
        (s["key"], ra, dec, kind, filter_name, bjd, bjd,
         json.dumps([node_id]), delta, float(s["mag"]),
         json.dumps({"cat_mag": s.get("cat_mag"), "cat_src": s.get("cat_src"),
                     "snr": s.get("snr"), "scheduled_seen": node_tier > 0}),
         now, now),
    )
    logger.info("Deviation detected (%s): %s mag=%.2f Δ=%s node=%s",
                kind, s["key"], float(s["mag"]),
                f"{delta:.2f}" if delta is not None else "—", node_id)
    return True


def _record_retrospective(node_id: str, bjd: float, filter_name: str, s: dict,
                          kind: str, delta, config: dict,
                          prov: dict) -> bool:
    """Open or update a retrospective discovery from an archive frame.

    Deliberately isolated from the live candidate flow: no corroboration gate,
    no promotion, and it NEVER calls reflex/interrupts — a nova found in an old
    image is a real record to crossmatch and credit, not a live event to chase.
    Deduped per (source_key, filter); keeps the peak brightness step.
    """
    ra, dec = float(s["ra"]), float(s["dec"])
    now = _now()
    existing = db.query_one(
        "SELECT id, delta_mag FROM retro_discoveries "
        "WHERE source_key = %s AND filter = %s AND state = 'detected' "
        "ORDER BY id DESC LIMIT 1", (s["key"], filter_name))
    dval = abs(delta) if delta is not None else 0.0
    if existing is not None:
        peak = max(float(existing.get("delta_mag") or 0.0), dval)
        db.execute(
            "UPDATE retro_discoveries SET delta_mag = %s, updated_at = %s "
            "WHERE id = %s", (peak, now, existing["id"]))
        return False
    db.execute(
        "INSERT INTO retro_discoveries "
        "(source_key, ra_deg, dec_deg, kind, filter, bjd, mag, delta_mag, "
        " node_id, contribution_id, user_id, state, detail, created_at, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'detected',%s,%s,%s)",
        (s["key"], ra, dec, kind, filter_name, bjd, float(s["mag"]), dval,
         node_id, prov.get("contribution_id"), str(prov.get("user_id") or ""),
         json.dumps({"cat_mag": s.get("cat_mag"), "cat_src": s.get("cat_src"),
                     "snr": s.get("snr")}), now, now))
    logger.info("Retrospective discovery (%s): %s mag=%.2f Δ=%.2f bjd=%.3f",
                kind, s["key"], float(s["mag"]), dval, bjd)
    return True


# ── Maintenance & admin ────────────────────────────────────────────────────────

def prune_survey_measurements(config: dict) -> int:
    """Delete raw survey rows past the retention window (default 120 days).
    The Welford aggregates in survey_sources keep the long-term science."""
    days = float(_cfg(config, "retention_days", 120))
    cutoff = datetime.fromtimestamp(
        time.time() - days * 86400, tz=timezone.utc).isoformat()
    conn = db.connect()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM survey_measurements WHERE received_at < %s",
                (cutoff,),
            )
            deleted = cur.rowcount
    finally:
        db.release(conn)
    if deleted:
        logger.info("Pruned %d survey measurements older than %.0f days",
                    deleted, days)
    return deleted


def stats() -> dict:
    """Counts for the admin surface / manage.py status."""
    out = {}
    out["sources"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM survey_sources") or {}).get("n", 0)
    out["measurements"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM survey_measurements") or {}).get("n", 0)
    out["candidates_open"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM discovery_candidates WHERE state IN %s",
        (OPEN_STATES,)) or {}).get("n", 0)
    out["candidates_review"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM discovery_candidates WHERE state = 'candidate'"
    ) or {}).get("n", 0)
    return out
