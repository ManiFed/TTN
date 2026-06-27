#!/usr/bin/env python3
"""
Node registry — registration, authentication, heartbeats.

Each node registers once with its location and telescope details, receives a
node_id + API key, and thereafter authenticates every call with the key.
On registration the cloud automatically fetches the light pollution value for
the node's location.
"""

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import time

from cloud import db, incidents
from cloud.conditions import fetch_light_pollution, fetch_light_pollution_detail
from src.shared_models import NodeInfo

logger = logging.getLogger("cloud.registry")

# How long after the last heartbeat a node is still considered online
HEARTBEAT_STALE_S = 900


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Registration ───────────────────────────────────────────────────────────────

def _bool(v) -> int:
    """Coerce a registration payload boolean field to 0/1 for SQLite."""
    return 1 if v and str(v).lower() not in ("0", "false", "no", "") else 0


def register_node(info: dict, lp_api_key: str = "") -> dict:
    """
    Register a new node (or re-register an existing one by node_id + api_key).

    Returns {"node_id": ..., "api_key": ...}.
    Raises ValueError on missing/invalid location.
    """
    node = NodeInfo.from_dict(info)

    if not (-90.0 <= node.latitude <= 90.0) or not (-180.0 <= node.longitude <= 180.0):
        raise ValueError("latitude/longitude missing or out of range")
    if node.latitude == 0.0 and node.longitude == 0.0:
        raise ValueError("latitude/longitude not set")

    # Re-registration: same node_id with matching key updates details in place
    existing = None
    if node.node_id:
        existing = db.query_one("SELECT * FROM nodes WHERE node_id = %s", (node.node_id,))
        if existing and existing["api_key"] != info.get("api_key", ""):
            raise ValueError("node_id already registered with a different API key")

    if existing:
        node_id, api_key = existing["node_id"], existing["api_key"]
    else:
        node_id = node.node_id or f"node_{secrets.token_hex(4)}"
        api_key = secrets.token_urlsafe(32)

    mpsas, bortle = fetch_light_pollution(node.latitude, node.longitude, lp_api_key)

    portable = _bool(info.get("portable", False))
    # Portable nodes start sleeping; fixed nodes start active.
    initial_status = "sleeping" if (portable and not existing) else "active"

    db.execute(
        """INSERT INTO nodes (
               node_id, api_key, owner_name, owner_email,
               latitude, longitude, elevation, city, country, utc_offset_hours,
               light_pollution_mpsas, bortle,
               tier, telescope_model, telescope_serial, telescope_name,
               aperture_mm, focal_length_mm, fov_deg,
               pixel_scale_arcsec, mount_type, max_exposure_s,
               camera_model, cooled_camera,
               filter_set, filters, mag_bright_limit, mag_faint_limit, min_altitude_deg,
               has_dew_heater, has_power_mgmt, has_enclosure, has_ups,
               horizon_mask, scheduling_notes, preferred_targets,
               portable, status, registered_at, last_heartbeat)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(node_id) DO UPDATE SET
               owner_name=excluded.owner_name, owner_email=excluded.owner_email,
               latitude=excluded.latitude, longitude=excluded.longitude,
               elevation=excluded.elevation, city=excluded.city,
               country=excluded.country, utc_offset_hours=excluded.utc_offset_hours,
               light_pollution_mpsas=excluded.light_pollution_mpsas,
               bortle=excluded.bortle,
               tier=excluded.tier,
               telescope_model=excluded.telescope_model,
               telescope_serial=excluded.telescope_serial,
               telescope_name=excluded.telescope_name,
               aperture_mm=excluded.aperture_mm,
               focal_length_mm=excluded.focal_length_mm, fov_deg=excluded.fov_deg,
               pixel_scale_arcsec=excluded.pixel_scale_arcsec,
               mount_type=excluded.mount_type,
               max_exposure_s=excluded.max_exposure_s,
               camera_model=excluded.camera_model,
               cooled_camera=excluded.cooled_camera,
               filter_set=excluded.filter_set,
               filters=excluded.filters,
               mag_bright_limit=excluded.mag_bright_limit,
               mag_faint_limit=excluded.mag_faint_limit,
               min_altitude_deg=excluded.min_altitude_deg,
               has_dew_heater=excluded.has_dew_heater,
               has_power_mgmt=excluded.has_power_mgmt,
               has_enclosure=excluded.has_enclosure,
               has_ups=excluded.has_ups,
               horizon_mask=excluded.horizon_mask,
               scheduling_notes=excluded.scheduling_notes,
               preferred_targets=excluded.preferred_targets,
               portable=excluded.portable,
               last_heartbeat=excluded.last_heartbeat""",
        (
            node_id, api_key, node.owner_name, node.owner_email,
            node.latitude, node.longitude, node.elevation,
            node.city, node.country, node.utc_offset_hours,
            mpsas, bortle,
            int(info.get("tier", 1)),
            node.telescope_model,
            str(info.get("telescope_serial", "") or ""),
            str(info.get("telescope_name", "") or ""),
            node.aperture_mm, node.focal_length_mm, node.fov_deg,
            node.pixel_scale_arcsec,
            str(info.get("mount_type", "alt_az") or "alt_az"),
            node.max_exposure_s,
            str(info.get("camera_model", "") or ""),
            _bool(info.get("cooled_camera")),
            str(info.get("filter_set", '["CV"]') or '["CV"]'),
            node.filters,
            node.mag_bright_limit, node.mag_faint_limit, node.min_altitude_deg,
            _bool(info.get("has_dew_heater")),
            _bool(info.get("has_power_mgmt")),
            _bool(info.get("has_enclosure")),
            _bool(info.get("has_ups")),
            str(info.get("horizon_mask", "[]") or "[]"),
            str(info.get("scheduling_notes", "") or ""),
            str(info.get("preferred_targets", "[]") or "[]"),
            portable, initial_status, _now(), _now(),
        ),
    )
    logger.info(
        "Node %s %s: Tier %d %s @ %.4f,%.4f  (%.1f mpsas, Bortle %d)",
        node_id, "updated" if existing else "registered",
        int(info.get("tier", 1)), node.telescope_model,
        node.latitude, node.longitude, mpsas, bortle,
    )
    return {"node_id": node_id, "api_key": api_key}


# ── Authentication ─────────────────────────────────────────────────────────────

def authenticate(node_id: str, api_key: str) -> Optional[dict]:
    """Return the node row when node_id + api_key are valid, else None."""
    if not node_id or not api_key:
        return None
    row = db.query_one("SELECT * FROM nodes WHERE node_id = %s", (node_id,))
    if row is None or not secrets.compare_digest(row["api_key"], api_key):
        return None
    return row


# ── Heartbeats ─────────────────────────────────────────────────────────────────

def heartbeat(node_id: str, conditions: Optional[dict] = None) -> None:
    """Record a heartbeat, optionally with current local conditions
    (sky temperature, detected cloud, safety state, utc_offset_hours, ...).

    Does not override vacation or disabled status — those are app-managed states.
    Wakes a sleeping portable node to active when it starts heartbeating.
    """
    params: list = [_now()]
    sql = ("UPDATE nodes SET last_heartbeat = %s, "
           "status = CASE WHEN status IN ('vacation', 'disabled') THEN status ELSE 'active' END")
    if conditions:
        sql += ", last_conditions = %s"
        params.append(json.dumps(conditions))
        offset = conditions.get("utc_offset_hours")
        if isinstance(offset, (int, float)) and -14.0 <= offset <= 14.0:
            sql += ", utc_offset_hours = %s"
            params.append(float(offset))
    sql += " WHERE node_id = %s"
    params.append(node_id)
    db.execute(sql, tuple(params))


def get_node(node_id: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM nodes WHERE node_id = %s", (node_id,))


def list_nodes(active_only: bool = False) -> list:
    rows = db.query("SELECT * FROM nodes ORDER BY registered_at")
    if active_only:
        rows = [r for r in rows if is_online(r)]
    return rows


def is_online(node_row: dict) -> bool:
    """True when the node has heartbeated recently and is not in a non-observing state."""
    status = node_row.get("status", "active")
    if status in ("disabled", "sleeping", "vacation"):
        return False
    hb = node_row.get("last_heartbeat")
    if not hb:
        return False
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(hb)).total_seconds()
    except ValueError:
        return False
    return age < HEARTBEAT_STALE_S


def public_view(node_row: dict) -> dict:
    """Node row without the API key (and without raw owner email), for status APIs."""
    out = {k: v for k, v in node_row.items() if k not in ("api_key", "owner_email")}
    out["online"] = is_online(node_row)
    out["portable"] = bool(node_row.get("portable"))
    out["last_conditions"] = db.loads(node_row.get("last_conditions"), {})
    out["previous_locations"] = db.loads(node_row.get("previous_locations"), [])
    return out


def refresh_node_performance(node_id: str) -> dict:
    """
    Recompute performance statistics for one node from its measurement history
    and update the nodes table.  Called by the nightly maintenance loop.

    Returns the updated stats dict so callers can log or surface them.

    Reliability formula (0..1):
        For nodes with < 10 observations → 0.50 (not enough data)
        Otherwise:
            0.40 × aavso_acceptance_rate        (do good data reach AAVSO?)
          + 0.25 × (1 − outlier_rate)           (does this node agree with others?)
          + 0.20 × min(1, clear_nights_30d/30)  (how often is it actually observing?)
          + 0.15 × precision_factor             (how precise is its photometry?)

        precision_factor = max(0, 1 − mean_uncertainty / 0.30)
        (0.30 mag is the AAVSO quality-gate ceiling; perfect = 0 uncertainty)

    Scheduler trust is reliability minus recent classified incident penalty.
    System-caused incidents do not penalize the node; environmental incidents
    are light; node-attributed incidents carry the strongest penalty.
    """
    totals = db.query_one(
        """SELECT
               COUNT(*)                                                AS total,
               SUM(aavso_submitted)                                    AS accepted,
               SUM(CASE WHEN validation_status='outlier' THEN 1 ELSE 0 END) AS outliers,
               AVG(CASE WHEN quality_flag != 'poor' THEN uncertainty END)   AS mean_unc,
               AVG(CASE WHEN quality_flag != 'poor' AND fwhm IS NOT NULL
                         THEN fwhm END)                                AS mean_fwhm
           FROM measurements WHERE node_id = %s""",
        (node_id,),
    ) or {}

    total    = int(totals.get("total",    0) or 0)
    accepted = int(totals.get("accepted", 0) or 0)
    outliers = int(totals.get("outliers", 0) or 0)
    mean_unc  = float(totals.get("mean_unc",  0.0) or 0.0)
    mean_fwhm = float(totals.get("mean_fwhm", 0.0) or 0.0)

    # Observations that were good quality but flagged as outliers by cross-validation
    # (i.e. they disagreed with other nodes' simultaneous measurements)
    rejected_row = db.query_one(
        """SELECT COUNT(*) AS n FROM measurements
           WHERE node_id = %s AND validation_status = 'outlier'
             AND quality_flag IN ('good', 'acceptable')""",
        (node_id,),
    ) or {}
    rejected = int((rejected_row or {}).get("n", 0) or 0)

    acceptance_rate = accepted / total if total > 0 else 0.0
    outlier_rate    = outliers / total if total > 0 else 0.0

    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    clear_row = db.query_one(
        """SELECT COUNT(DISTINCT date(received_at)) AS n
           FROM measurements WHERE node_id = %s AND received_at >= %s""",
        (node_id, cutoff_30d),
    ) or {}
    clear_nights = int((clear_row or {}).get("n", 0) or 0)

    if total < 10:
        reliability = 0.5   # insufficient data — neutral score
    else:
        precision_factor = max(0.0, 1.0 - mean_unc / 0.30) if mean_unc > 0 else 0.5
        reliability = (
            0.40 * acceptance_rate
            + 0.25 * (1.0 - outlier_rate)
            + 0.20 * min(1.0, clear_nights / 30.0)
            + 0.15 * precision_factor
        )
        reliability = round(max(0.0, min(1.0, reliability)), 4)
    incident_penalty = incidents.recent_scheduler_penalty(node_id)
    scheduler_trust = round(max(0.0, min(1.0, reliability * (1.0 - incident_penalty))), 4)

    db.execute(
        """UPDATE nodes SET
               total_observations = %s,
               aavso_accepted     = %s,
               aavso_rejected     = %s,
               mean_uncertainty   = %s,
               mean_fwhm          = %s,
               clear_nights_30d   = %s,
               outlier_rate       = %s,
               reliability_score  = %s,
               scheduler_trust_score = %s,
               perf_updated_at    = %s
           WHERE node_id = %s""",
        (total, accepted, rejected, round(mean_unc, 4), round(mean_fwhm, 2),
         clear_nights, round(outlier_rate, 4), reliability, scheduler_trust, _now(), node_id),
    )
    logger.info(
        "Performance refresh %s: %d obs, accepted=%d, outlier_rate=%.2f, "
        "clear_30d=%d, reliability=%.3f, trust=%.3f",
        node_id, total, accepted, outlier_rate, clear_nights, reliability, scheduler_trust,
    )
    return {
        "node_id":           node_id,
        "total_observations": total,
        "aavso_accepted":    accepted,
        "aavso_rejected":    rejected,
        "mean_uncertainty":  mean_unc,
        "mean_fwhm":         mean_fwhm,
        "clear_nights_30d":  clear_nights,
        "outlier_rate":      outlier_rate,
        "reliability_score": reliability,
        "scheduler_trust_score": scheduler_trust,
        "incident_penalty": incident_penalty,
    }


def refresh_all_performance() -> int:
    """Refresh performance stats for every active node.  Returns node count."""
    nodes = db.query("SELECT node_id FROM nodes WHERE status != 'disabled'")
    for n in nodes:
        try:
            refresh_node_performance(n["node_id"])
        except Exception as exc:
            logger.error("Performance refresh failed for %s: %s", n["node_id"], exc)
    return len(nodes)


def refresh_light_pollution(lp_api_key: str = "") -> None:
    """
    Periodic re-fetch of light pollution for every node (monthly cadence is
    plenty — VIIRS data updates annually). Stores mpsas, bortle, and source.
    Sleeps 3 s between nodes to avoid rate-limiting Clear Outside (fallback source).
    """
    for i, row in enumerate(db.query("SELECT node_id, latitude, longitude FROM nodes")):
        if i > 0:
            time.sleep(3)
        result = fetch_light_pollution_detail(row["latitude"], row["longitude"], lp_api_key)
        db.execute(
            "UPDATE nodes SET light_pollution_mpsas = %s, bortle = %s WHERE node_id = %s",
            (result["mpsas"], result["bortle"], row["node_id"]),
        )
        logger.info("LP refresh %s: %.2f mpsas Bortle %d [%s]",
                    row["node_id"], result["mpsas"], result["bortle"], result["source"])


# ── Session management (portable nodes) ───────────────────────────────────────

def _update_previous_locations(node_id: str, lat: float, lon: float,
                                city: str, site_name: str) -> None:
    """Prepend tonight's location to the node's previous-locations list.

    Deduplicates by city name (case-insensitive).  Keeps the 10 most recent.
    """
    row = db.query_one("SELECT previous_locations FROM nodes WHERE node_id = %s", (node_id,))
    if row is None:
        return
    locations: list = db.loads(row.get("previous_locations"), [])
    entry = {
        "lat": lat, "lon": lon,
        "city": city, "site_name": site_name,
        "last_used": _now(),
    }
    city_lc = city.lower()
    locations = [loc for loc in locations if loc.get("city", "").lower() != city_lc]
    locations.insert(0, entry)
    locations = locations[:10]
    db.execute(
        "UPDATE nodes SET previous_locations = %s WHERE node_id = %s",
        (json.dumps(locations), node_id),
    )


def start_session(node_id: str, lat: float, lon: float, city: str,
                  site_name: str, lp_api_key: str = "") -> dict:
    """
    Activate a portable node for tonight's observing session.

    Updates the session location, fetches sky quality for that location,
    sets status → active, and prepends the location to previous_locations.

    Returns {"mpsas": float, "bortle": int} for the session site.
    """
    node = db.query_one("SELECT portable FROM nodes WHERE node_id = %s", (node_id,))
    if node is None:
        raise ValueError(f"node not found: {node_id}")
    if not node.get("portable"):
        raise ValueError("start_session is only valid for portable nodes")

    mpsas, bortle = fetch_light_pollution(lat, lon, lp_api_key)
    db.execute(
        """UPDATE nodes SET
               session_lat = %s, session_lon = %s,
               session_city = %s, session_site_name = %s,
               light_pollution_mpsas = %s, bortle = %s,
               status = 'active', last_heartbeat = %s
           WHERE node_id = %s""",
        (lat, lon, city, site_name, mpsas, bortle, _now(), node_id),
    )
    _update_previous_locations(node_id, lat, lon, city, site_name)
    logger.info(
        "Session started: node %s @ %s (%.4f,%.4f) %.1f mpsas Bortle %d",
        node_id, city or site_name, lat, lon, mpsas, bortle,
    )
    return {"mpsas": mpsas, "bortle": bortle}


def end_session(node_id: str) -> None:
    """Explicitly end a portable node's session — sets status back to sleeping."""
    db.execute(
        "UPDATE nodes SET status = 'sleeping', session_lat = 0, session_lon = 0,"
        " session_city = '', session_site_name = '' WHERE node_id = %s AND portable = 1",
        (node_id,),
    )
    logger.info("Session ended: node %s → sleeping", node_id)


def mark_stale_portables_sleeping() -> int:
    """Set portable nodes whose heartbeat has gone stale back to sleeping.

    Called by the nightly maintenance loop so a portable node that was left
    running and lost connection doesn't stay 'active' forever.
    Returns the number of nodes updated.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_STALE_S)).isoformat()
    rows = db.query(
        "SELECT node_id FROM nodes WHERE portable = 1 AND status = 'active'"
        " AND (last_heartbeat IS NULL OR last_heartbeat < %s)",
        (cutoff,),
    )
    for r in rows:
        db.execute(
            "UPDATE nodes SET status = 'sleeping' WHERE node_id = %s",
            (r["node_id"],),
        )
        logger.info("Portable node %s heartbeat stale → sleeping", r["node_id"])
    return len(rows)


# ── Vacation management ────────────────────────────────────────────────────────

def set_vacation(node_id: str, until_date: str) -> None:
    """Pause a node until *until_date* (ISO date string 'YYYY-MM-DD').

    Missed nights during vacation are excluded from the reliability score.
    """
    db.execute(
        "UPDATE nodes SET status = 'vacation', vacation_until = %s WHERE node_id = %s",
        (until_date, node_id),
    )
    logger.info("Node %s on vacation until %s", node_id, until_date)


def clear_vacation(node_id: str) -> None:
    """Cancel an active vacation.  Portable nodes return to sleeping; fixed to offline."""
    node = db.query_one("SELECT portable FROM nodes WHERE node_id = %s", (node_id,))
    if node is None:
        return
    new_status = "sleeping" if node.get("portable") else "offline"
    db.execute(
        "UPDATE nodes SET status = %s, vacation_until = '' WHERE node_id = %s",
        (new_status, node_id),
    )
    logger.info("Node %s vacation cleared → %s", node_id, new_status)


def expire_vacations() -> int:
    """Auto-clear vacations whose end date has passed.  Called by nightly maintenance."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT node_id, portable FROM nodes"
        " WHERE status = 'vacation' AND vacation_until != '' AND vacation_until < %s",
        (today,),
    )
    for r in rows:
        new_status = "sleeping" if r.get("portable") else "offline"
        db.execute(
            "UPDATE nodes SET status = %s, vacation_until = '' WHERE node_id = %s",
            (new_status, r["node_id"]),
        )
        logger.info("Node %s vacation expired → %s", r["node_id"], new_status)
    return len(rows)
