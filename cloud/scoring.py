#!/usr/bin/env python3
"""
Scoring engine — composite score for every (target, node) pair.

    total = w_brightness · brightness_match
          + w_science    · scientific_value
          + w_time       · time_criticality
          + w_coverage   · coverage_gap
          + w_observe    · observability
          + w_roi        · science_roi

Observability is itself a weighted blend of: light pollution penalty against
target magnitude, weather (cloud forecast over the coming night), moon
interference (illumination × proximity), best achievable airmass, visibility
window length, and telescope match (FoV / aperture suitability for the
target).  Every component is normalised to 0..1 so the weights in cloud
config read directly as relative importance.

Scores are persisted to the `scores` table; the scheduler reads them back.
score_all() runs on the scheduled loop and after each alert ingestion.
"""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from cloud import db, registry, tuning
from cloud.conditions import (
    airmass_from_alt, altitude_curve, angular_separation_deg,
    astro_cloud_cover_at, cloud_cover_at, fetch_astronomy_weather,
    fetch_weather, moon_state, night_window,
    seeing_score_at, transparency_score_at,
)

logger = logging.getLogger("cloud.scoring")

DEFAULT_WEIGHTS = {
    "brightness": 0.20,
    "science":    0.25,
    "time":       0.15,
    "coverage":   0.15,
    "observe":    0.25,
    "roi":        0.10,
}

# The observability sub-weights are auto-tuned: they are seeded from
# config.yaml (scoring.observability_weights) but read live from the DB on every
# run via tuning.active_obs_weights(), so the nightly Claude monitor can adjust
# them without a restart.  See cloud/tuning.py.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _live_composite_weights(config: dict) -> dict:
    """Live (auto-tuned) composite weights, seeded from config. Falls back to
    config + defaults if the tuning state can't be read (e.g. no DB in tests),
    preserving the pre-tuning contract."""
    try:
        return {**DEFAULT_WEIGHTS, **tuning.active_composite_weights(config)}
    except Exception:
        return {**DEFAULT_WEIGHTS, **(config.get("scoring", {}).get("weights", {}) or {})}


# ── Component scores ───────────────────────────────────────────────────────────

def brightness_match(target_mag: Optional[float], node: dict) -> float:
    """1.0 in the sweet spot of the node's magnitude range, falling off
    toward the saturation and faint limits."""
    if target_mag is None:
        return 0.5   # unknown brightness — neither favour nor exclude
    bright = float(node["mag_bright_limit"])
    faint = float(node["mag_faint_limit"])
    if target_mag < bright - 1.0 or target_mag > faint + 0.5:
        return 0.0
    mid = (bright + faint) / 2.0
    half = max(1.0, (faint - bright) / 2.0)
    return max(0.0, 1.0 - ((target_mag - mid) / half) ** 2 * 0.7)


def time_criticality(target: dict) -> float:
    """Hot for time-critical targets in the first days after discovery,
    decaying with age; baseline for everything else."""
    base = 0.6 if target.get("time_critical") else 0.2
    try:
        discovered = datetime.fromisoformat(target["discovered_at"])
        age_days = (datetime.now(timezone.utc) - discovered).total_seconds() / 86400.0
    except (KeyError, TypeError, ValueError):
        return base
    if target.get("time_critical"):
        # Fresh transients: 1.0 on day 0 → ~0.4 by day 14
        return max(0.3, 1.0 * math.exp(-age_days / 12.0))
    return base


def historical_neglect(target: dict, window_days: int = 30) -> float:
    """Fraction of expected observations missed over a rolling window.

    Blends two signals (equal weight when external data is available):
      internal — our own measurements vs cadence-based expected count
      external — global community coverage via AAVSO / ALeRCE

    The external signal prevents the snowball effect where a target scores as
    neglected only because our nodes were clouded out.
    """
    from cloud import external_coverage

    cadence_h = max(1.0, float(target.get("cadence_hours", 24.0)))
    expected = window_days * 24.0 / cadence_h
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    row = db.query_one(
        "SELECT COUNT(*) AS cnt FROM measurements "
        "WHERE target_name = %s AND received_at >= %s",
        (target["name"], cutoff),
    )
    actual = float(row["cnt"]) if row and row["cnt"] else 0.0
    internal = max(0.0, min(1.0, 1.0 - actual / max(1.0, expected)))

    ext = external_coverage.external_neglect(target, window_days)
    if ext is not None:
        return 0.5 * internal + 0.5 * ext
    return internal


def coverage_gap(target: dict) -> float:
    """Blends short-term recency (40%) with long-term historical neglect weighted
    by science priority (60%).  High score means important, under-observed target."""
    cadence_h = max(1.0, float(target.get("cadence_hours", 24.0)))
    row = db.query_one(
        "SELECT MAX(received_at) AS last FROM measurements WHERE target_name = %s",
        (target["name"],),
    )
    if not row or not row["last"]:
        recency = 1.0
    else:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(row["last"])).total_seconds() / 3600.0
        except ValueError:
            recency = 1.0
        else:
            recency = max(0.0, min(1.0, age_h / (2.0 * cadence_h)))

    neglect = historical_neglect(target)
    science = float(target.get("priority", 0.5))
    return 0.4 * recency + 0.6 * (neglect * science)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def science_roi(target: dict, window_days: int = 90) -> float:
    """Balanced science-yield score for a target from recent network outcomes.

    Blends accepted-data yield, cadence-gap closure, and time-critical success.
    Sparse histories are pulled toward 0.5 so new targets/classes remain viable.
    """
    name = target.get("name")
    if not name:
        return 0.5

    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    rows = db.query(
        """SELECT received_at, aavso_submitted, validation_status, quality_flag
           FROM measurements
           WHERE target_name = %s AND received_at >= %s
           ORDER BY received_at ASC LIMIT 200""",
        (name, cutoff),
    )
    total = len(rows)
    if total == 0:
        return 0.5

    accepted = sum(
        1 for row in rows
        if int(row.get("aavso_submitted") or 0) == 1
        and row.get("validation_status") != "outlier"
        and row.get("quality_flag") in ("good", "acceptable")
    )
    accepted_yield = accepted / total

    cadence_h = max(1.0, float(target.get("cadence_hours", 24.0) or 24.0))
    gap_closures = 0
    prev = None
    for row in rows:
        try:
            when = datetime.fromisoformat(row["received_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if prev is None or (when - prev).total_seconds() / 3600.0 >= cadence_h * 0.75:
            gap_closures += 1
        prev = when
    gap_value = gap_closures / total

    timely = 0.5
    if target.get("time_critical"):
        try:
            discovered = datetime.fromisoformat(target["discovered_at"])
        except (KeyError, TypeError, ValueError):
            timely = accepted_yield
        else:
            urgent_rows = []
            for row in rows:
                try:
                    when = datetime.fromisoformat(row["received_at"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= (when - discovered).total_seconds() <= 72 * 3600:
                    urgent_rows.append(row)
            if urgent_rows:
                urgent_accepted = sum(
                    1 for row in urgent_rows
                    if int(row.get("aavso_submitted") or 0) == 1
                    and row.get("validation_status") != "outlier"
                    and row.get("quality_flag") in ("good", "acceptable")
                )
                timely = urgent_accepted / len(urgent_rows)
            else:
                timely = 0.5

    raw = 0.45 * accepted_yield + 0.35 * gap_value + 0.20 * timely
    evidence = min(1.0, total / 20.0)
    return round(_clamp01(0.5 * (1.0 - evidence) + raw * evidence), 4)


def light_pollution_factor(target_mag: Optional[float], node: dict) -> float:
    """How well this sky supports this target.  Bright targets shrug off light
    pollution; faint ones need dark skies.  mpsas 22=pristine, 17=inner city."""
    mpsas = float(node.get("light_pollution_mpsas", 20.0))
    sky = max(0.0, min(1.0, (mpsas - 17.0) / 5.0))     # 0 awful → 1 pristine
    if target_mag is None:
        return 0.5 + 0.5 * sky
    faint = float(node["mag_faint_limit"])
    # headroom: how far from the node's faint limit the target sits (0..1)
    headroom = max(0.0, min(1.0, (faint - target_mag) / 6.0))
    return max(0.0, min(1.0, 0.3 + 0.7 * (sky * (1.0 - headroom) + headroom)))


def weather_factor(node: dict, night: Optional[tuple]) -> float:
    """
    Composite astronomy-weather score for the coming night (0 = terrible, 1 = perfect).

    Blends three components weighted equally:
      - clear-sky fraction (1 - cloud_cover)
      - seeing quality (atmospheric steadiness)
      - transparency (atmospheric extinction)

    Prefers 7timer ASTRO data (seeing + transparency + astronomy-specific cloud
    cover); falls back to Open-Meteo cloud cover when 7timer is unavailable.
    """
    if night is None:
        return 0.0

    lat, lon = node["latitude"], node["longitude"]
    astro = fetch_astronomy_weather(lat, lon)
    generic = fetch_weather(lat, lon) if astro is None else None

    t0, t1 = night
    cloud_samples, seeing_samples, transp_samples = [], [], []
    t = t0
    while t <= t1:
        # cloud cover — prefer 7timer, fall back to Open-Meteo
        if astro is not None:
            cc = astro_cloud_cover_at(astro, t)
        else:
            cc = cloud_cover_at(generic, t)
        if cc is not None:
            cloud_samples.append(1.0 - cc)

        if astro is not None:
            s = seeing_score_at(astro, t)
            if s is not None:
                seeing_samples.append(s)
            tr = transparency_score_at(astro, t)
            if tr is not None:
                transp_samples.append(tr)

        t += timedelta(hours=1)

    clear = sum(cloud_samples) / len(cloud_samples) if cloud_samples else 0.5
    seeing = sum(seeing_samples) / len(seeing_samples) if seeing_samples else None
    transp = sum(transp_samples) / len(transp_samples) if transp_samples else None

    if seeing is not None and transp is not None:
        # All three components available — equal weight blend
        return (clear + seeing + transp) / 3.0
    # Only cloud data available
    return clear


def moon_factor(target: dict, night: Optional[tuple]) -> float:
    """Penalty for a bright moon close to the target at mid-night."""
    if night is None:
        return 0.0
    mid = night[0] + (night[1] - night[0]) / 2
    moon = moon_state(mid)
    sep = angular_separation_deg(
        target["ra_deg"], target["dec_deg"], moon["ra_deg"], moon["dec_deg"])
    illum = moon["illumination"]
    if sep < 10.0:
        return 0.05
    # Interference falls off with separation, scaled by illumination
    proximity = max(0.0, 1.0 - sep / 90.0)
    return max(0.0, min(1.0, 1.0 - illum * proximity))


def telescope_match(target: dict, node: dict) -> float:
    """How well the registered telescope suits this target class.  Small
    wide-field instruments (Seestar) are great for bright variables and
    nearby SNe, weak for faint point sources needing resolution."""
    aperture = float(node.get("aperture_mm", 50.0))
    score = 0.7
    ttype = target.get("target_type", "unknown")
    mag = target.get("mag")
    if mag is not None:
        # Rough aperture-limited magnitude: Seestar(50mm)~15.5, +5 log(D ratio)
        practical_limit = 15.5 + 5.0 * math.log10(max(aperture, 10.0) / 50.0)
        score = 1.0 if mag < practical_limit - 1.5 else (
            0.7 if mag < practical_limit - 0.5 else 0.35)
    if ttype in ("EB", "CV", "VAR") and float(node.get("fov_deg", 1.27)) >= 1.0:
        score = min(1.0, score + 0.1)   # wide field = easy comp stars
    return score


# ── Observability + composite ─────────────────────────────────────────────────

def observability(target: dict, node: dict, night: Optional[tuple],
                  weather: float, obs_weights: dict) -> tuple:
    """
    Returns (observability_score, visibility_minutes, best_alt_deg).
    Zero when the target never clears the node's minimum altitude tonight.
    """
    if night is None:
        return 0.0, 0.0, -90.0

    curve = altitude_curve(
        target["ra_deg"], target["dec_deg"],
        node["latitude"], node["longitude"], night[0], night[1], step_min=15)
    min_alt = float(node.get("min_altitude_deg", 25.0))
    visible = [(t, a) for t, a in curve if a >= min_alt]
    if not visible:
        return 0.0, 0.0, max(a for _, a in curve) if curve else -90.0

    best_alt = max(a for _, a in visible)
    vis_min = len(visible) * 15.0
    night_min = max(1.0, (night[1] - night[0]).total_seconds() / 60.0)

    airmass = airmass_from_alt(best_alt)
    f_airmass = max(0.0, min(1.0, (3.0 - airmass) / 2.0))     # X=1 → 1.0, X=3 → 0
    f_window = min(1.0, vis_min / min(night_min, 240.0))      # ≥4 h visible = 1.0
    f_lp = light_pollution_factor(target.get("mag"), node)
    f_moon = moon_factor(target, night)
    f_scope = telescope_match(target, node)

    w = obs_weights
    total_w = sum(w.values()) or 1.0
    score = (w["light_pollution"] * f_lp + w["weather"] * weather
             + w["moon"] * f_moon + w["airmass"] * f_airmass
             + w["window"] * f_window + w["telescope"] * f_scope) / total_w
    return score, vis_min, best_alt


def score_target_for_node(target: dict, node: dict, night: Optional[tuple],
                          weather: float, config: dict,
                          obs_weights: Optional[dict] = None,
                          weights: Optional[dict] = None) -> dict:
    """Full composite score with component breakdown.

    obs_weights and weights, when provided, are the live (auto-tuned)
    observability sub-weights and composite weights; score_all fetches each once
    per run to avoid a DB read per (target, node) pair.  When omitted (ad-hoc
    callers) they are read from the DB-backed tuning state.
    """
    if weights is None:
        weights = _live_composite_weights(config)
    if obs_weights is None:
        obs_weights = tuning.active_obs_weights(config)

    obs, vis_min, best_alt = observability(target, node, night, weather, obs_weights)
    components = {
        "brightness": brightness_match(target.get("mag"), node),
        "science":    float(target.get("priority", 0.5)),
        "time":       time_criticality(target),
        "coverage":   coverage_gap(target),
        "observe":    obs,
        "roi":        science_roi(target),
        "visibility_minutes": vis_min,
        "best_alt_deg": round(best_alt, 1),
    }
    # A target that can't be seen tonight scores zero regardless of value
    if obs <= 0.0:
        total = 0.0
    else:
        total_w = sum(weights.values()) or 1.0
        total = sum(weights[k] * components[k]
                    for k in ("brightness", "science", "time", "coverage", "observe", "roi")) / total_w

        # Apply learned scheduler trust as a multiplier.
        # scheduler_trust_score is 0..1; new nodes start at 0.5.
        # Scaling: total × (0.5 + 0.5 × reliability) means:
        #   trust=1.0 → ×1.00 (no penalty)
        #   trust=0.5 → ×0.75 (new/unknown node — slight preference for proven ones)
        #   trust=0.0 → ×0.50 (persistently poor node — still gets some assignments)
        trust = float(
            node.get("scheduler_trust_score")
            if node.get("scheduler_trust_score") is not None
            else node.get("reliability_score", 0.5)
            or 0.5
        )
        total = total * (0.5 + 0.5 * trust)
        components["reliability_score"] = round(float(node.get("reliability_score", trust) or trust), 3)
        components["scheduler_trust_score"] = round(trust, 3)

    components["total"] = round(total, 4)
    components["explanation"] = explain_score(target, node, components, weights)
    return components


def explain_score(target: dict, node: dict, components: dict, weights: dict) -> dict:
    """Compact, client-safe explanation of a target/node score."""
    labels = {
        "brightness": "brightness match",
        "science": "science priority",
        "time": "urgency",
        "coverage": "coverage & neglect",
        "observe": "observability",
        "roi": "science ROI",
    }
    ranked = sorted(
        (
            {
                "key": key,
                "label": labels[key],
                "value": round(float(components.get(key, 0.0)), 3),
                "weight": round(float(weights.get(key, 0.0)), 3),
                "contribution": round(
                    float(components.get(key, 0.0)) * float(weights.get(key, 0.0)), 3
                ),
            }
            for key in labels
        ),
        key=lambda item: item["contribution"],
        reverse=True,
    )
    top = ranked[:3]
    reason_bits = [f"{i['label']} {i['value']:.2f}" for i in top if i["value"] > 0]
    if float(components.get("observe", 0.0)) <= 0:
        summary = "Not observable from this node tonight."
    elif reason_bits:
        summary = "Strongest factors: " + ", ".join(reason_bits) + "."
    else:
        summary = "Low but schedulable score."
    return {
        "summary": summary,
        "factors": ranked,
        "best_alt_deg": components.get("best_alt_deg"),
        "visibility_minutes": components.get("visibility_minutes"),
        "reliability_score": components.get("reliability_score"),
        "scheduler_trust_score": components.get("scheduler_trust_score"),
        "roi_score": components.get("roi"),
        "target_type": target.get("target_type", "unknown"),
        "node_id": node.get("node_id", ""),
    }


def score_all(config: dict) -> int:
    """
    Score every active target against every active node and persist results.
    Returns the number of (target, node) pairs scored.  Runs on the scheduler
    loop and after alert ingestion.
    """
    targets = db.query("SELECT * FROM targets WHERE active = 1")
    nodes = registry.list_nodes()
    nodes = [n for n in nodes if n.get("status") != "disabled"]
    if not targets or not nodes:
        logger.info("Scoring skipped — %d targets, %d nodes", len(targets), len(nodes))
        return 0

    # Read the live (auto-tuned) weights once for the whole run.
    obs_weights = tuning.active_obs_weights(config)
    weights = _live_composite_weights(config)

    count = 0
    for node in nodes:
        night = night_window(node["latitude"], node["longitude"])
        weather = weather_factor(node, night)
        for target in targets:
            try:
                comp = score_target_for_node(
                    target, node, night, weather, config, obs_weights, weights)
            except Exception as exc:
                logger.warning("Scoring failed %s @ %s: %s",
                               target["name"], node["node_id"], exc)
                continue
            db.execute(
                """INSERT INTO scores (target_id, node_id, scored_at, total, components)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT(target_id, node_id) DO UPDATE SET
                       scored_at=excluded.scored_at, total=excluded.total,
                       components=excluded.components""",
                (target["target_id"], node["node_id"], _now(),
                 comp["total"], json.dumps(comp)),
            )
            count += 1
        logger.info("Scored %d targets for %s (weather=%.2f, night=%s)",
                    len(targets), node["node_id"], weather,
                    "yes" if night else "none")
    return count
