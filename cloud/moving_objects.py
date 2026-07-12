"""
Asteroid / minor-planet astrometry — moving-object detection.

Every plate-solved frame's full-frame survey scan (cloud.survey.ingest_batch)
already includes uncatalogued point sources alongside the catalog-matched
stars. cloud.survey keys those by *quantized sky position* for its own
stationary-object discovery pipeline, so a moving object gets a fresh key
every frame and never accumulates there — see _survey_source_key in
src/photometry.py. This module keeps the raw per-frame detections instead,
links them across frames of the same field/night into linear tracklets,
crossmatches survivors against known objects (IMCCE SkyBoT) to filter out
already-catalogued asteroids, and hands genuinely unknown tracklets to a
human for confirm/reject — the same shape as cloud.crossmatch's
discovery_candidates lifecycle, but for moving positions instead of
stationary ones:

    record_frame_detections()  → moving_object_detections (raw, per-frame)
    link_tracklets()            → asteroid_candidates (state='linked')
    crossmatch_pending()        → state='known_skybot' | 'candidate'
    confirm_candidate()         → state='confirmed', cloud.mpc_report writes
                                   an ADES astrometry report for manual
                                   submission to the Minor Planet Center
    reject_candidate()          → state='rejected'

Two classifications ride along with linking, both best-effort heuristics
rather than physical models:

  priority ('normal' | 'neo_candidate') — set from the tracklet's on-sky
    rate. A rate above mpc.neo_rate_threshold_deg_day is far faster than a
    typical main-belt asteroid at opposition (~0.3-0.5 deg/day) and is
    consistent with a close-approaching NEO, which won't stay in this field
    for another night. crossmatch_pending processes these first and, if one
    survives the SkyBoT check, fires an urgent same-night interrupt (see
    _fire_neo_alert) rather than waiting for a human to notice it in the
    review queue.

  object_type ('asteroid' | 'comet_candidate') — set from the fraction of a
    tracklet's detections flagged "extended" by src/photometry.py's
    per-frame sharpness heuristic (a marginally resolved coma is flatter
    than the frame's stellar PSF). This is a soft signal from a stellar
    point-source finder, not a coma/tail model — it will miss diffuse
    comets DAOStarFinder never detects as sources in the first place, and
    can be fooled by a blended background galaxy or double star. Treat
    comet_candidate as "worth a human's second look", not a confirmed comet.

confirm_candidate() also schedules same-night rotation-light-curve
follow-up (schedule_rotation_followup / dispatch_due_followups) for
confirmed, non-NEO objects — repeat exposures a few hours apart build an
amplitude/period curve, the same kind of science an asteroid's rotation
period comes from, without needing CHORUS's fixed-RA/Dec/multi-night
cadence machinery (a short linear-rate extrapolation is only valid across
one night, not the weeks CHORUS schedules on).
"""

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import psycopg2.extras

from cloud import db

logger = logging.getLogger("cloud.moving_objects")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg(config: dict, key: str, default):
    return (config.get("mpc") or {}).get(key, default)


def _sky_sep_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle separation in degrees (haversine — safe at any RA/Dec,
    including across the RA=0/360 seam)."""
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    dd = d2 - d1
    dr = r2 - r1
    a = math.sin(dd / 2.0) ** 2 + math.cos(d1) * math.cos(d2) * math.sin(dr / 2.0) ** 2
    return math.degrees(2.0 * math.asin(min(1.0, math.sqrt(a))))


def _ra_diff_deg(ra_to: float, ra_from: float) -> float:
    """Signed shortest-path RA difference in degrees, wraparound-safe (two
    points at RA=359.5 and RA=0.5 are 1 deg apart, not 359)."""
    return (ra_to - ra_from + 180.0) % 360.0 - 180.0


# ── Recording ────────────────────────────────────────────────────────────────

def record_frame_detections(node_id: str, bjd: float, filter_name: str,
                            frame_id: str, date_obs_utc: str,
                            sources: list, config: dict) -> int:
    """Stash this frame's uncatalogued point sources for tracklet linking.

    `sources` is cloud.survey.ingest_batch's already-validated `new_sources`
    list (both catalog-matched and unmatched); only unmatched ("matched":
    False) entries above the SNR floor are worth keeping here — a matched
    source is a known star, not a moving-object candidate.
    """
    min_snr = float(_cfg(config, "min_detection_snr", 5.0))
    now = _now()
    rows = []
    for s in sources:
        if s.get("matched"):
            continue
        try:
            snr = float(s.get("snr") or 0.0)
            if snr < min_snr:
                continue
            rows.append((node_id, bjd, float(s["ra"]), float(s["dec"]),
                        float(s["mag"]), float(s.get("mag_err") or 0.05),
                        snr, filter_name, frame_id, date_obs_utc,
                        bool(s.get("extended", False)), now))
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return 0

    conn = db.connect()
    try:
        with conn:
            cur = conn.cursor()
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO moving_object_detections "
                "(node_id, bjd, ra_deg, dec_deg, mag, mag_err, snr, filter, "
                " frame_id, date_obs_utc, extended, created_at) VALUES %s "
                "ON CONFLICT (node_id, bjd, ra_deg, dec_deg) DO NOTHING",
                rows,
            )
    finally:
        db.release(conn)
    return len(rows)


# ── Tracklet linking ───────────────────────────────────────────────────────────

def _cluster_by_time_and_field(dets: list, window_days: float,
                               max_sep_deg: float) -> list:
    """Single-linkage clustering: a detection joins a cluster if it falls
    within window_days AND max_sep_deg of any existing cluster member.
    Volumes here are small (one node's unlinked detections per pass), so a
    plain O(n^2) scan is cheap."""
    clusters: list = []
    for d in dets:
        placed = False
        for cluster in clusters:
            for m in cluster:
                if (abs(d["bjd"] - m["bjd"]) <= window_days
                        and _sky_sep_deg(d["ra_deg"], d["dec_deg"],
                                         m["ra_deg"], m["dec_deg"]) <= max_sep_deg):
                    cluster.append(d)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            clusters.append([d])
    return clusters


def _find_linear_track(cluster: list, max_resid_arcsec: float,
                       min_pts: int) -> Optional[list]:
    """Largest subset of >=min_pts points consistent with one linear
    sky-motion track. Cluster sizes are small (single field, single night),
    so a plain point-pair-then-vote search is cheap — no orbit-fitting
    machinery needed to reject noise from a real, near-linear short arc."""
    best = None
    n = len(cluster)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = cluster[i], cluster[j]
            dt = b["bjd"] - a["bjd"]
            if dt <= 0:
                continue
            ra_rate = _ra_diff_deg(b["ra_deg"], a["ra_deg"]) / dt
            dec_rate = (b["dec_deg"] - a["dec_deg"]) / dt
            support = [a, b]
            for k in range(n):
                if k in (i, j):
                    continue
                c = cluster[k]
                dt_c = c["bjd"] - a["bjd"]
                pred_ra = a["ra_deg"] + ra_rate * dt_c
                pred_dec = a["dec_deg"] + dec_rate * dt_c
                sep_arcsec = _sky_sep_deg(pred_ra, pred_dec,
                                          c["ra_deg"], c["dec_deg"]) * 3600.0
                if sep_arcsec <= max_resid_arcsec:
                    support.append(c)
            if len(support) >= min_pts and (best is None or len(support) > len(best)):
                best = support
    return best


def _sky_rate_deg_day(ra_rate: float, dec_rate: float, dec0: float) -> float:
    """Scalar on-sky angular rate in deg/day, correcting the RA rate for its
    cos(dec) foreshortening (a raw hypot of ra_rate/dec_rate overstates speed
    away from the equator)."""
    return math.hypot(ra_rate * math.cos(math.radians(dec0)), dec_rate)


def _classify_track(track: list, dec0: float, ra_rate: float, dec_rate: float,
                    config: dict) -> tuple:
    """(priority, object_type) for a newly-linked or newly-merged tracklet.
    See the module docstring for what these heuristics can and can't catch."""
    neo_threshold = float(_cfg(config, "neo_rate_threshold_deg_day", 2.0))
    priority = ("neo_candidate"
               if _sky_rate_deg_day(ra_rate, dec_rate, dec0) >= neo_threshold
               else "normal")

    comet_fraction_thr = float(_cfg(config, "comet_extended_fraction", 0.6))
    n_extended = sum(1 for d in track if d.get("extended"))
    object_type = ("comet_candidate"
                  if track and (n_extended / len(track)) >= comet_fraction_thr
                  else "asteroid")
    return priority, object_type


def _save_tracklet(node_id: str, track: list, config: dict) -> bool:
    track = sorted(track, key=lambda d: d["bjd"])
    bjds = [float(d["bjd"]) for d in track]
    ra0, dec0 = float(track[0]["ra_deg"]), float(track[0]["dec_deg"])
    dt = np.array([b - bjds[0] for b in bjds], dtype=float)
    ra_rel = np.array([_ra_diff_deg(float(d["ra_deg"]), ra0) for d in track], dtype=float)
    dec_rel = np.array([float(d["dec_deg"]) - dec0 for d in track], dtype=float)

    if np.ptp(dt) > 0:
        ra_rate, ra_b = np.polyfit(dt, ra_rel, 1)
        dec_rate, dec_b = np.polyfit(dt, dec_rel, 1)
    else:
        ra_rate = dec_rate = ra_b = dec_b = 0.0

    resid_arcsec = [
        _sky_sep_deg(ra0 + (ra_rate * dt[k] + ra_b), dec0 + (dec_rate * dt[k] + dec_b),
                    float(track[k]["ra_deg"]), float(track[k]["dec_deg"])) * 3600.0
        for k in range(len(track))
    ]

    priority, object_type = _classify_track(track, dec0, float(ra_rate), float(dec_rate), config)

    now = _now()
    detection_ids = [d["id"] for d in track]
    mean_mag = float(np.mean([float(d["mag"]) for d in track]))
    cand_id = db.execute(
        "INSERT INTO asteroid_candidates "
        "(node_id, first_bjd, last_bjd, n_detections, ra0_deg, dec0_deg, "
        " ra_rate_deg_day, dec_rate_deg_day, fit_residual_arcsec, mean_mag, "
        " priority, object_type, state, detail, created_at, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'linked',%s,%s,%s)",
        (node_id, bjds[0], bjds[-1], len(track), ra0 % 360.0, dec0,
         float(ra_rate), float(dec_rate), float(np.mean(resid_arcsec)), mean_mag,
         priority, object_type,
         json.dumps({"detection_ids": detection_ids}), now, now),
        returning_id=True,
    )
    db.execute(
        "UPDATE moving_object_detections SET tracklet_id = %s WHERE id = ANY(%s)",
        (cand_id, detection_ids))
    logger.info(
        "Tracklet #%s linked: %d detections, node=%s, rate=(%.4f,%.4f) deg/day, "
        "residual=%.2f\", priority=%s, object_type=%s", cand_id, len(track), node_id,
        ra_rate, dec_rate, float(np.mean(resid_arcsec)), priority, object_type)
    if priority == "neo_candidate":
        logger.warning(
            "FAST MOVER tracklet #%s: %.2f deg/day — flagged neo_candidate",
            cand_id, _sky_rate_deg_day(float(ra_rate), float(dec_rate), dec0))
    return True


def _retire_stale(config: dict) -> int:
    """Mark unlinked detections whose linking window has closed as done, so
    the linker's bounded scan set can never be starved by the permanent
    backlog of noise (single detections, cosmic rays) that will never form a
    tracklet. A detection can only gain a partner from frames ingested around
    the same wall-clock time — live frames arrive within minutes, an archive
    upload completes within hours — so anything unlinked after link_retry_hours
    from insertion (created_at) is terminal, regardless of its (possibly
    historical) bjd."""
    retry_hours = float(_cfg(config, "link_retry_hours", 12.0))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=retry_hours)).isoformat()
    conn = db.connect()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE moving_object_detections SET link_done = TRUE "
                "WHERE tracklet_id IS NULL AND link_done = FALSE "
                "  AND created_at < %s", (cutoff,))
            retired = cur.rowcount
    finally:
        db.release(conn)
    if retired:
        logger.info("Retired %d stale unlinked detection(s) past the "
                    "%.0f h linking window", retired, retry_hours)
    return retired


def link_tracklets(config: dict, limit: int = 500) -> dict:
    """Background-loop entry point: cluster unlinked detections by field/
    night, extract linear tracks, and open an asteroid_candidates row for
    each. Called every few minutes (cloud/main.py)."""
    window_days = float(_cfg(config, "link_window_hours", 6.0)) / 24.0
    max_sep_deg = float(_cfg(config, "link_max_field_sep_deg", 2.0))
    min_pts = int(_cfg(config, "min_track_detections", 3))
    max_resid_arcsec = float(_cfg(config, "max_fit_residual_arcsec", 5.0))

    # Drop the closed-window backlog first so the LIMIT below always spends
    # its budget on detections that can still link.
    _retire_stale(config)

    rows = db.query(
        "SELECT id, node_id, bjd, ra_deg, dec_deg, mag, mag_err, snr, filter, "
        "       extended "
        "FROM moving_object_detections "
        "WHERE tracklet_id IS NULL AND link_done = FALSE "
        "ORDER BY node_id, bjd LIMIT %s", (limit,))
    if not rows:
        return {"clusters": 0, "tracklets": 0}

    by_node: dict = {}
    for r in rows:
        by_node.setdefault(r["node_id"], []).append(r)

    n_clusters = n_tracklets = 0
    for node_id, dets in by_node.items():
        for cluster in _cluster_by_time_and_field(dets, window_days, max_sep_deg):
            n_clusters += 1
            if len(cluster) < min_pts:
                continue
            track = _find_linear_track(cluster, max_resid_arcsec, min_pts)
            if track is None:
                continue
            if _save_tracklet(node_id, track, config):
                n_tracklets += 1
    if n_tracklets:
        logger.info("Tracklet linking: %d cluster(s), %d tracklet(s)",
                    n_clusters, n_tracklets)
    return {"clusters": n_clusters, "tracklets": n_tracklets}


# ── Multi-night arc linking ─────────────────────────────────────────────────────

def _merge_tracklets(a: dict, b: dict, config: dict) -> bool:
    """Fold tracklet b's detections into a, refit the linear rate over the
    combined multi-night baseline, and mark b 'merged'. This is not a strict
    physical fit — real motion curves over multi-day baselines, which is why
    the caller matches on a loose predicted-position tolerance rather than a
    tight residual — but it's good enough for an ADES submission, which
    reports raw per-detection astrometry rather than this summary rate."""
    detail_a = db.loads(a.get("detail"), {})
    detail_b = db.loads(b.get("detail"), {})
    combined_ids = (detail_a.get("detection_ids") or []) + (detail_b.get("detection_ids") or [])
    if not combined_ids:
        return False

    dets = db.query(
        "SELECT id, bjd, ra_deg, dec_deg, mag, extended FROM moving_object_detections "
        "WHERE id = ANY(%s) ORDER BY bjd", (combined_ids,))
    if len(dets) < 2:
        return False

    bjds = [float(d["bjd"]) for d in dets]
    ra0, dec0 = float(dets[0]["ra_deg"]), float(dets[0]["dec_deg"])
    dt = np.array([t - bjds[0] for t in bjds], dtype=float)
    ra_rel = np.array([_ra_diff_deg(float(d["ra_deg"]), ra0) for d in dets], dtype=float)
    dec_rel = np.array([float(d["dec_deg"]) - dec0 for d in dets], dtype=float)
    if np.ptp(dt) > 0:
        ra_rate, ra_b = np.polyfit(dt, ra_rel, 1)
        dec_rate, dec_b = np.polyfit(dt, dec_rel, 1)
    else:
        ra_rate = dec_rate = ra_b = dec_b = 0.0
    resid_arcsec = [
        _sky_sep_deg(ra0 + (ra_rate * dt[k] + ra_b), dec0 + (dec_rate * dt[k] + dec_b),
                    float(dets[k]["ra_deg"]), float(dets[k]["dec_deg"])) * 3600.0
        for k in range(len(dets))
    ]
    merged_from = sorted(set((detail_a.get("merged_from") or [])
                             + (detail_b.get("merged_from") or []) + [b["id"]]))
    detail_a["detection_ids"] = combined_ids
    detail_a["merged_from"] = merged_from
    now = _now()
    priority, object_type = _classify_track(dets, dec0, float(ra_rate), float(dec_rate), config)

    conn = db.connect()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE asteroid_candidates SET first_bjd=%s, last_bjd=%s, "
                "n_detections=%s, ra0_deg=%s, dec0_deg=%s, ra_rate_deg_day=%s, "
                "dec_rate_deg_day=%s, fit_residual_arcsec=%s, mean_mag=%s, "
                "priority=%s, object_type=%s, "
                "detail=%s, updated_at=%s WHERE id=%s",
                (bjds[0], bjds[-1], len(dets), ra0 % 360.0, dec0,
                 float(ra_rate), float(dec_rate), float(np.mean(resid_arcsec)),
                 float(np.mean([float(d["mag"]) for d in dets])),
                 priority, object_type,
                 json.dumps(detail_a), now, a["id"]))
            cur.execute(
                "UPDATE asteroid_candidates SET state='merged', detail=%s, "
                "updated_at=%s WHERE id=%s",
                (json.dumps({**detail_b, "merged_into": a["id"]}), now, b["id"]))
            cur.execute(
                "UPDATE moving_object_detections SET tracklet_id=%s WHERE id = ANY(%s)",
                (a["id"], combined_ids))
    finally:
        db.release(conn)
    logger.info(
        "Tracklets #%s + #%s merged into multi-night arc #%s: %d night(s), "
        "%d detections spanning %.1f day(s)",
        a["id"], b["id"], a["id"], len(merged_from) + 1, len(dets), bjds[-1] - bjds[0])
    return True


def link_arcs(config: dict, limit: int = 200) -> dict:
    """Background-loop entry point: merge single-night tracklets of the same
    node into multi-night arcs when a later night's tracklet start position
    matches the earlier tracklet's linear-rate extrapolation. A stronger,
    multi-night astrometry set makes for a much better MPC submission than a
    lone 3-point single-night tracklet.

    Scoped to one node_id: MPC astrometry needs a per-station code, and this
    project has only ever registered a single global mpc.observatory_code
    (not one per node — see cloud/config.yaml), so merging arcs across nodes
    would misattribute observations to the wrong station.

    Only 'linked' and 'candidate' tracklets are eligible. 'known_skybot' is
    left alone (a known object doesn't need a stronger orbit from us), and
    'confirmed'/'rejected' are left alone because a human verdict is final —
    an operator who wants a later night folded into an already-confirmed
    report can still find it via detail.merged_from after the fact.
    """
    window_days = float(_cfg(config, "arc_link_window_days", 14.0))
    max_resid_arcsec = float(_cfg(config, "arc_max_pred_resid_arcsec", 20.0))
    max_sep_deg = float(_cfg(config, "link_max_field_sep_deg", 2.0))

    rows = db.query(
        "SELECT * FROM asteroid_candidates WHERE state IN ('linked','candidate') "
        "ORDER BY node_id, first_bjd LIMIT %s", (limit,))
    by_node: dict = {}
    for r in rows:
        by_node.setdefault(r["node_id"], []).append(r)

    merges = 0
    for node_id, cands in by_node.items():
        cands = sorted(cands, key=lambda c: c["first_bjd"])
        merged_ids: set = set()
        for i, a in enumerate(cands):
            if a["id"] in merged_ids:
                continue
            for b in cands[i + 1:]:
                if b["id"] in merged_ids:
                    continue
                dt = float(b["first_bjd"]) - float(a["last_bjd"])
                if dt <= 0 or dt > window_days:
                    continue
                if _sky_sep_deg(float(a["ra0_deg"]), float(a["dec0_deg"]),
                                float(b["ra0_deg"]), float(b["dec0_deg"])) > max_sep_deg:
                    continue
                dt_from_a0 = float(b["first_bjd"]) - float(a["first_bjd"])
                pred_ra = (float(a["ra0_deg"])
                          + float(a.get("ra_rate_deg_day") or 0.0) * dt_from_a0)
                pred_dec = (float(a["dec0_deg"])
                           + float(a.get("dec_rate_deg_day") or 0.0) * dt_from_a0)
                resid_arcsec = _sky_sep_deg(
                    pred_ra, pred_dec, float(b["ra0_deg"]), float(b["dec0_deg"])) * 3600.0
                if resid_arcsec > max_resid_arcsec:
                    continue
                if _merge_tracklets(a, b, config):
                    merged_ids.add(b["id"])
                    merges += 1
                    # Refresh a's in-memory row so a third night can chain
                    # onto this arc within the same pass.
                    a = db.query_one(
                        "SELECT * FROM asteroid_candidates WHERE id = %s", (a["id"],))
    if merges:
        logger.info("Arc linking: merged %d multi-night tracklet(s)", merges)
    return {"merged": merges}


# ── Known-object crossmatch ─────────────────────────────────────────────────────

def _bjd_tdb_to_jd_utc(bjd_tdb: float, ra_deg: float, dec_deg: float) -> float:
    """Recover the geocentric JD(UTC) of observation from a BJD_TDB by
    inverting the barycentric Rømer + TDB terms applied forward in
    src/photometry.py:_compute_bjd_ex. SkyBoT expects the *observation* epoch
    (UTC); passing the barycentric BJD instead misplaces the ephemeris by up
    to ~8.3 min of light-travel time — tens of arcsec for a fast mover, enough
    to miss a real match within the cone. Evaluated at the geocenter (the
    observer-on-Earth term is ≤21 ms, negligible for a 30\" cone)."""
    from astropy.time import Time
    from astropy.coordinates import EarthLocation, SkyCoord
    import astropy.units as u

    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    geocenter = EarthLocation.from_geocentric(0.0 * u.m, 0.0 * u.m, 0.0 * u.m)
    t_tdb = Time(bjd_tdb, format="jd", scale="tdb")
    guess = t_tdb
    for _ in range(3):
        ltt = guess.utc.light_travel_time(target, kind="barycentric", location=geocenter)
        guess = t_tdb - ltt
    return float(guess.utc.jd)


def _skybot_lookup(ra_deg: float, dec_deg: float, epoch_bjd: float,
                   radius_arcsec: float) -> str:
    """Nearest known solar-system object within the cone at this epoch, or
    ''. IMCCE SkyBoT plays the same role here that VSX/TNS play in
    cloud.crossmatch, but for minor planets."""
    from astropy.time import Time
    from astropy.coordinates import SkyCoord
    from astroquery.imcce import Skybot
    import astropy.units as u

    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    # SkyBoT wants the observation epoch in UTC, not the barycentric BJD.
    try:
        jd_utc = _bjd_tdb_to_jd_utc(epoch_bjd, ra_deg, dec_deg)
    except Exception as exc:
        logger.debug("BJD->UTC conversion failed (%s) — using BJD as epoch", exc)
        jd_utc = epoch_bjd
    epoch = Time(jd_utc, format="jd", scale="utc")
    try:
        table = Skybot.cone_search(coord, radius_arcsec * u.arcsec, epoch)
    except Exception as exc:
        if "No solar system object" in str(exc):
            return ""
        raise
    if table is None or len(table) == 0:
        return ""
    return str(table[0]["Name"])


def _fire_neo_alert(cand: dict, ra_deg: float, dec_deg: float, config: dict) -> bool:
    """Urgent same-night reobservation for a fast-mover that just survived
    SkyBoT: a genuine NEO candidate moves too fast to wait for the normal
    human-review queue — by the time someone looks, it may have left the
    field or (if close-approaching) simply moved on. Mirrors
    cloud.reflex._create_interrupt's shape (interrupts table + live push to
    dark/online nodes), independent of the reflex/discovery_candidates flow
    since this fires from asteroid_candidates, not discovery_candidates."""
    from cloud import live
    from cloud.server import _eligible_interrupt_nodes

    dark = live.dark_online_nodes()
    name = f"neo:{cand['id']}"
    target = {"name": name, "ra_deg": ra_deg, "dec_deg": dec_deg,
             "mag": cand.get("mean_mag")}
    try:
        ranked = _eligible_interrupt_nodes(target, min_score=0.25)
    except Exception as exc:
        logger.debug("NEO alert eligibility failed: %s", exc)
        ranked = []
    picked = [nid for nid in ranked if nid in dark] or list(dark)
    n_nodes = int(_cfg(config, "neo_alert_n_nodes", 2))
    picked = picked[:max(1, n_nodes)]
    if not picked:
        logger.info("NEO candidate #%s: no dark node available for urgent "
                    "reobservation", cand["id"])
        return False

    expires_hours = float(_cfg(config, "neo_alert_expires_hours", 1.5))
    expires = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()
    iid = db.execute(
        """INSERT INTO interrupts
               (target_id, name, ra_deg, dec_deg, mag, reason, node_ids,
                created_at, expires_at)
           VALUES (NULL,%s,%s,%s,%s,'neo_followup',%s,%s,%s)""",
        (name, ra_deg, dec_deg, cand.get("mean_mag"), json.dumps(picked),
         _now(), expires),
        returning_id=True,
    )
    for nid in picked:
        try:
            live.publish(nid, "interrupt",
                        {"reason": "neo_followup", "candidate_id": cand["id"]})
        except Exception as exc:  # pragma: no cover
            logger.debug("NEO alert publish to %s failed: %s", nid, exc)
    logger.warning("NEO urgent reobservation #%s: candidate #%s → %d node(s) %s",
                   iid, cand["id"], len(picked), picked)
    return True


def crossmatch_pending(config: dict, limit: int = 25) -> dict:
    """Background-loop entry point: crossmatch linked tracklets against
    known objects. Survivors become 'candidate' — awaiting human review."""
    radius = float(_cfg(config, "skybot_radius_arcsec", 30.0))
    rows = db.query(
        "SELECT * FROM asteroid_candidates WHERE state = 'linked' "
        "ORDER BY (priority = 'neo_candidate') DESC, updated_at ASC LIMIT %s",
        (limit,))
    checked = known = promoted = 0

    for cand in rows:
        checked += 1
        now = _now()
        first_bjd = float(cand["first_bjd"])
        mid_bjd = (first_bjd + float(cand["last_bjd"])) / 2.0
        dt_mid = mid_bjd - first_bjd
        ra_mid = float(cand["ra0_deg"]) + float(cand.get("ra_rate_deg_day") or 0.0) * dt_mid
        dec_mid = float(cand["dec0_deg"]) + float(cand.get("dec_rate_deg_day") or 0.0) * dt_mid
        try:
            name = _skybot_lookup(ra_mid, dec_mid, mid_bjd, radius)
        except Exception as exc:
            logger.warning("SkyBoT lookup failed for candidate #%s: %s",
                           cand["id"], exc)
            continue

        if name:
            known += 1
            db.execute(
                "UPDATE asteroid_candidates SET state = 'known_skybot', "
                "skybot_name = %s, updated_at = %s WHERE id = %s",
                (name, now, cand["id"]))
            logger.info("Tracklet #%s is known object %s", cand["id"], name)
            continue

        promoted += 1
        db.execute(
            "UPDATE asteroid_candidates SET state = 'candidate', "
            "updated_at = %s WHERE id = %s", (now, cand["id"]))
        logger.warning(
            "UNKNOWN MOVING OBJECT survives SkyBoT crossmatch → candidate #%s "
            "at (%.5f, %+.5f), rate=(%.4f,%.4f) deg/day — awaiting human review",
            cand["id"], ra_mid, dec_mid,
            cand.get("ra_rate_deg_day") or 0.0, cand.get("dec_rate_deg_day") or 0.0)

        if cand.get("priority") == "neo_candidate":
            try:
                _fire_neo_alert(cand, ra_mid, dec_mid, config)
            except Exception as exc:
                logger.warning("NEO urgent alert failed for candidate #%s: %s",
                               cand["id"], exc)

    if checked:
        logger.info("SkyBoT crossmatch: %d checked, %d known, %d promoted",
                    checked, known, promoted)
    return {"checked": checked, "known": known, "candidates": promoted}


# ── Human verdicts ───────────────────────────────────────────────────────────

def confirm_candidate(cand_id: int, config: dict, note: str = "") -> Optional[dict]:
    """Human verdict: this tracklet is a genuine, previously-unknown moving
    object. Generates an MPC astrometry report for manual submission. No
    targets/CHORUS hookup — an asteroid isn't a revisit target the way a
    variable-star candidate is."""
    cand = db.query_one("SELECT * FROM asteroid_candidates WHERE id = %s",
                        (cand_id,))
    if cand is None or cand["state"] not in ("candidate", "linked"):
        return None

    year = datetime.now(timezone.utc).year
    designation = f"BS-MP {year}-{cand_id:04d}"
    now = _now()
    detail = db.loads(cand.get("detail"), {})
    if note:
        detail["confirm_note"] = note
    db.execute(
        "UPDATE asteroid_candidates SET state = 'confirmed', designation = %s, "
        "detail = %s, updated_at = %s WHERE id = %s",
        (designation, json.dumps(detail), now, cand_id))
    logger.warning("MOVING OBJECT CONFIRMED: candidate #%s → %s", cand_id, designation)

    report = None
    try:
        from cloud import mpc_report
        report = mpc_report.generate_report(cand_id, config)
    except Exception as exc:
        logger.warning("MPC report generation failed for candidate #%s: %s",
                       cand_id, exc)

    followups = 0
    try:
        followups = schedule_rotation_followup(cand_id, config)
    except Exception as exc:
        logger.warning("Rotation follow-up scheduling failed for candidate "
                       "#%s: %s", cand_id, exc)

    return {"candidate_id": cand_id, "designation": designation, "report": report,
            "followups_scheduled": followups}


def reject_candidate(cand_id: int, note: str = "") -> bool:
    cand = db.query_one("SELECT * FROM asteroid_candidates WHERE id = %s",
                        (cand_id,))
    if cand is None:
        return False
    detail = db.loads(cand.get("detail"), {})
    if note:
        detail["reject_note"] = note
    db.execute(
        "UPDATE asteroid_candidates SET state = 'rejected', detail = %s, "
        "updated_at = %s WHERE id = %s",
        (json.dumps(detail), _now(), cand_id))
    logger.info("Asteroid candidate #%s rejected%s", cand_id,
               f" ({note})" if note else "")
    return True


# ── Rotation light-curve follow-up ──────────────────────────────────────────

def _rotation_cfg(config: dict) -> dict:
    r = (config.get("mpc") or {}).get("rotation_followup") or {}
    return {
        "enabled":       bool(r.get("enabled", False)),
        "n_visits":      int(r.get("n_visits", 4)),
        "interval_hours": float(r.get("interval_hours", 1.5)),
        "expires_hours":  float(r.get("expires_hours", 0.75)),
    }


def schedule_rotation_followup(cand_id: int, config: dict) -> int:
    """Queue same-night repeat exposures of a just-confirmed asteroid so its
    brightness variation over the night can be turned into a rotation light
    curve. Only for 'asteroid'/'normal' candidates — a neo_candidate moves
    too fast for a multi-hour linear extrapolation to stay useful, and a
    comet_candidate's coma brightness isn't a rotation signal. Slots are
    inserted now but each position is computed later, at dispatch time (see
    dispatch_due_followups), from the same linear rate the tracklet was
    fit with."""
    cfg = _rotation_cfg(config)
    if not cfg["enabled"]:
        return 0
    cand = db.query_one("SELECT * FROM asteroid_candidates WHERE id = %s",
                        (cand_id,))
    if cand is None or cand["state"] != "confirmed":
        return 0
    if cand.get("priority") == "neo_candidate" or cand.get("object_type") != "asteroid":
        logger.info("Rotation follow-up skipped for candidate #%s "
                    "(priority=%s, object_type=%s)", cand_id,
                    cand.get("priority"), cand.get("object_type"))
        return 0

    now = _now()
    scheduled = 0
    for seq in range(1, cfg["n_visits"] + 1):
        not_before = (datetime.now(timezone.utc)
                     + timedelta(hours=cfg["interval_hours"] * seq)).isoformat()
        db.execute(
            "INSERT INTO asteroid_followups "
            "(candidate_id, node_id, seq, not_before, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (cand_id, cand["node_id"], seq, not_before, now))
        scheduled += 1
    logger.info("Rotation follow-up scheduled: candidate #%s, %d visit(s) "
               "every %.1fh on node %s", cand_id, scheduled,
               cfg["interval_hours"], cand["node_id"])
    return scheduled


def _extrapolate_now(cand: dict) -> tuple:
    """Candidate's predicted (ra, dec) at the current moment from its linear
    fit. Treats "now" as a JD close enough to the tracklet's BJD_TDB baseline
    that the light-travel/TDB-UTC offset (well under a minute) is negligible
    against an hours-scale extrapolation for a slow (non-NEO) mover — see
    _bjd_tdb_to_jd_utc for the more careful conversion used where arcsecond
    precision actually matters (SkyBoT crossmatch, MPC astrometry)."""
    from astropy.time import Time
    now_jd = Time.now().jd
    dt = now_jd - float(cand["first_bjd"])
    ra = float(cand["ra0_deg"]) + float(cand.get("ra_rate_deg_day") or 0.0) * dt
    dec = float(cand["dec0_deg"]) + float(cand.get("dec_rate_deg_day") or 0.0) * dt
    return ra % 360.0, dec


def dispatch_due_followups(config: dict, limit: int = 50) -> dict:
    """Background-loop entry point: fire any rotation follow-up slot whose
    time has come as an ordinary interrupt, positioned via the candidate's
    linear-rate extrapolation to right now (not the position at confirm
    time, which could be stale by hours). Called from cloud/main.py
    alongside the other moving-object passes."""
    cfg = _rotation_cfg(config)
    rows = db.query(
        "SELECT * FROM asteroid_followups WHERE fired_at IS NULL "
        "AND not_before <= %s ORDER BY not_before LIMIT %s", (_now(), limit))
    fired = skipped = 0
    for row in rows:
        now = _now()
        cand = db.query_one("SELECT * FROM asteroid_candidates WHERE id = %s",
                            (row["candidate_id"],))
        if cand is None or cand["state"] != "confirmed":
            db.execute(
                "UPDATE asteroid_followups SET fired_at = %s WHERE id = %s",
                (now, row["id"]))
            skipped += 1
            continue

        ra, dec = _extrapolate_now(cand)
        expires = (datetime.now(timezone.utc)
                  + timedelta(hours=cfg["expires_hours"])).isoformat()
        name = f"asteroid_followup:{cand['id']}:{row['seq']}"
        iid = db.execute(
            """INSERT INTO interrupts
                   (target_id, name, ra_deg, dec_deg, mag, reason, node_ids,
                    created_at, expires_at)
               VALUES (NULL,%s,%s,%s,%s,'asteroid_followup',%s,%s,%s)""",
            (name, ra, dec, cand.get("mean_mag"),
             json.dumps([row["node_id"]]), now, expires),
            returning_id=True,
        )
        db.execute(
            "UPDATE asteroid_followups SET fired_at = %s, interrupt_id = %s "
            "WHERE id = %s", (now, iid, row["id"]))
        try:
            from cloud import live
            live.publish(row["node_id"], "interrupt",
                        {"reason": "asteroid_followup", "candidate_id": cand["id"]})
        except Exception as exc:  # pragma: no cover
            logger.debug("Follow-up publish to %s failed: %s", row["node_id"], exc)
        fired += 1
        logger.info("Rotation follow-up #%s fired: candidate #%s visit %d "
                   "→ node %s at (%.5f, %+.5f)", iid, cand["id"], row["seq"],
                   row["node_id"], ra, dec)
    if fired or skipped:
        logger.info("Rotation follow-up dispatch: %d fired, %d skipped",
                    fired, skipped)
    return {"fired": fired, "skipped": skipped}


def prune_detections(config: dict) -> int:
    """Bound the high-volume detection table. Deletes rows older than
    detection_retention_days that are either retired noise (link_done) or
    belong to a closed-negative tracklet (rejected / known_skybot). Detections
    of still-open (linked / candidate) and confirmed tracklets are kept — the
    latter are needed to (re)generate the MPC report. Called from the daily
    maintenance loop (cloud/main.py), mirroring survey.prune_survey_measurements."""
    days = float(_cfg(config, "detection_retention_days", 30))
    cutoff = datetime.fromtimestamp(
        time.time() - days * 86400, tz=timezone.utc).isoformat()
    conn = db.connect()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM moving_object_detections "
                "WHERE created_at < %s AND (link_done = TRUE OR tracklet_id IN "
                "  (SELECT id FROM asteroid_candidates "
                "   WHERE state IN ('rejected', 'known_skybot')))",
                (cutoff,))
            deleted = cur.rowcount
    finally:
        db.release(conn)
    if deleted:
        logger.info("Pruned %d moving-object detection(s) older than %.0f days",
                    deleted, days)
    return deleted


def stats() -> dict:
    """Counts for the admin surface."""
    out = {}
    out["detections_unlinked"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM moving_object_detections "
        "WHERE tracklet_id IS NULL AND link_done = FALSE") or {}).get("n", 0)
    out["tracklets_open"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM asteroid_candidates "
        "WHERE state IN ('linked','candidate')") or {}).get("n", 0)
    out["candidates_review"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM asteroid_candidates "
        "WHERE state = 'candidate'") or {}).get("n", 0)
    out["confirmed"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM asteroid_candidates "
        "WHERE state = 'confirmed'") or {}).get("n", 0)
    out["multi_night_arcs"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM asteroid_candidates "
        "WHERE state IN ('linked','candidate','confirmed') "
        "  AND detail::jsonb ? 'merged_from'") or {}).get("n", 0)
    out["neo_candidates"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM asteroid_candidates "
        "WHERE priority = 'neo_candidate' "
        "  AND state IN ('linked','candidate','confirmed')") or {}).get("n", 0)
    out["comet_candidates"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM asteroid_candidates "
        "WHERE object_type = 'comet_candidate' "
        "  AND state IN ('linked','candidate','confirmed')") or {}).get("n", 0)
    out["followups_pending"] = (db.query_one(
        "SELECT COUNT(*) AS n FROM asteroid_followups "
        "WHERE fired_at IS NULL") or {}).get("n", 0)
    return out
