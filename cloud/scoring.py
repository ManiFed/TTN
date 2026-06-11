#!/usr/bin/env python3
"""
Scoring engine — composite score for every (target, node) pair.

    total = w_brightness · brightness_match
          + w_science    · scientific_value
          + w_time       · time_criticality
          + w_coverage   · coverage_gap
          + w_observe    · observability

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

from cloud import db, registry
from cloud.conditions import (
    airmass_from_alt, altitude_curve, angular_separation_deg,
    cloud_cover_at, fetch_weather, moon_state, night_window,
)

logger = logging.getLogger("cloud.scoring")

DEFAULT_WEIGHTS = {
    "brightness": 0.20,
    "science":    0.25,
    "time":       0.15,
    "coverage":   0.15,
    "observe":    0.25,
}

DEFAULT_OBS_WEIGHTS = {
    "light_pollution": 0.20,
    "weather":         0.25,
    "moon":            0.15,
    "airmass":         0.15,
    "window":          0.15,
    "telescope":       0.10,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def coverage_gap(target: dict) -> float:
    """1.0 when the network has no recent data on this target, dropping to 0
    when it has been measured more recently than the desired cadence."""
    cadence_h = max(1.0, float(target.get("cadence_hours", 24.0)))
    row = db.query_one(
        "SELECT MAX(received_at) AS last FROM measurements WHERE target_name = ?",
        (target["name"],),
    )
    if not row or not row["last"]:
        return 1.0
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(row["last"])).total_seconds() / 3600.0
    except ValueError:
        return 1.0
    return max(0.0, min(1.0, age_h / (2.0 * cadence_h)))


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
    """Mean forecast clear-sky fraction over the coming night (0 = overcast)."""
    if night is None:
        return 0.0
    forecast = fetch_weather(node["latitude"], node["longitude"])
    if forecast is None:
        return 0.5   # no forecast — neutral
    t0, t1 = night
    samples, t = [], t0
    while t <= t1:
        cc = cloud_cover_at(forecast, t)
        if cc is not None:
            samples.append(1.0 - cc)
        t += timedelta(hours=1)
    return sum(samples) / len(samples) if samples else 0.5


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
                          weather: float, config: dict) -> dict:
    """Full composite score with component breakdown."""
    weights = {**DEFAULT_WEIGHTS, **config.get("scoring", {}).get("weights", {})}
    obs_weights = {**DEFAULT_OBS_WEIGHTS,
                   **config.get("scoring", {}).get("observability_weights", {})}

    obs, vis_min, best_alt = observability(target, node, night, weather, obs_weights)
    components = {
        "brightness": brightness_match(target.get("mag"), node),
        "science":    float(target.get("priority", 0.5)),
        "time":       time_criticality(target),
        "coverage":   coverage_gap(target),
        "observe":    obs,
        "visibility_minutes": vis_min,
        "best_alt_deg": round(best_alt, 1),
    }
    # A target that can't be seen tonight scores zero regardless of value
    if obs <= 0.0:
        total = 0.0
    else:
        total_w = sum(weights.values()) or 1.0
        total = sum(weights[k] * components[k]
                    for k in ("brightness", "science", "time", "coverage", "observe")) / total_w
    components["total"] = round(total, 4)
    return components


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

    count = 0
    for node in nodes:
        night = night_window(node["latitude"], node["longitude"])
        weather = weather_factor(node, night)
        for target in targets:
            try:
                comp = score_target_for_node(target, node, night, weather, config)
            except Exception as exc:
                logger.warning("Scoring failed %s @ %s: %s",
                               target["name"], node["node_id"], exc)
                continue
            db.execute(
                """INSERT INTO scores (target_id, node_id, scored_at, total, components)
                   VALUES (?,?,?,?,?)
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
