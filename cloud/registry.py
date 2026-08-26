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


def auto_tier(filter_set) -> int:
    """Capability tier from the filter complement (VISION.md): a rig with at
    least three of Johnson-Cousins B/V/R/I does real multi-band photometry —
    tier 2. Everything else that observes is tier 1 (tier 0 = contributor,
    tier 3 = spectroscopy, assigned manually)."""
    if isinstance(filter_set, str):
        try:
            filter_set = json.loads(filter_set or "[]")
        except ValueError:
            filter_set = []
    bands = {str(f).strip().upper() for f in (filter_set or [])}
    return 2 if len(bands & {"B", "V", "R", "I"}) >= 3 else 1


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
        recovery_token = existing.get("recovery_token") or secrets.token_urlsafe(32)
    else:
        node_id = node.node_id or f"node_{secrets.token_hex(4)}"
        api_key = secrets.token_urlsafe(32)
        # Lets the node agent silently recover from a revoked api_key later
        # (see rekey_node) without losing this node_id's history -- kept
        # separately from api_key so routine traffic never exposes it.
        recovery_token = secrets.token_urlsafe(32)

    mpsas, bortle = fetch_light_pollution(node.latitude, node.longitude, lp_api_key)

    portable = _bool(info.get("portable", False))
    contributor = _bool(info.get("contributor", False))
    # Portable nodes start sleeping — the owner explicitly connects/turns them
    # on (e.g. from the Tonight tab) once set up at a site; fixed nodes start
    # active. Contributor nodes are tier-0 virtual instruments: they upload
    # survey frames from existing images but are never scheduled by CHORUS.
    if contributor and not existing:
        initial_status = "contributor"
    else:
        initial_status = "sleeping" if (portable and not existing) else "active"
    vacation_until = ""
    vacation_from = ""
    if contributor:
        tier = 0
    else:
        tier = max(int(info.get("tier", 1)),
                   auto_tier(info.get("filter_set", '["CV"]')))
    mount_type = ("none" if contributor
                  else str(info.get("mount_type", "alt_az") or "alt_az"))

    db.execute(
        """INSERT INTO nodes (
               node_id, api_key, recovery_token, owner_name, owner_email,
               latitude, longitude, elevation, city, country, utc_offset_hours,
               light_pollution_mpsas, bortle,
               tier, telescope_model, telescope_serial, telescope_name,
               aperture_mm, focal_length_mm, fov_deg,
               pixel_scale_arcsec, mount_type, max_exposure_s,
               camera_model, cooled_camera,
               filter_set, filters, mag_bright_limit, mag_faint_limit, min_altitude_deg,
               has_dew_heater, has_power_mgmt, has_enclosure, has_ups,
               horizon_mask, scheduling_notes, preferred_targets,
               portable, status, vacation_until, vacation_from, registered_at, last_heartbeat)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
            node_id, api_key, recovery_token, node.owner_name, node.owner_email,
            node.latitude, node.longitude, node.elevation,
            node.city, node.country, node.utc_offset_hours,
            mpsas, bortle,
            tier,
            node.telescope_model,
            str(info.get("telescope_serial", "") or ""),
            str(info.get("telescope_name", "") or ""),
            node.aperture_mm, node.focal_length_mm, node.fov_deg,
            node.pixel_scale_arcsec,
            mount_type,
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
            portable, initial_status, vacation_until, vacation_from, _now(), _now(),
        ),
    )
    logger.info(
        "Node %s %s: Tier %d %s (%.1f mpsas, Bortle %d)",
        node_id, "updated" if existing else "registered",
        tier, node.telescope_model,
        mpsas, bortle,
    )
    return {"node_id": node_id, "api_key": api_key, "recovery_token": recovery_token}


# ── Authentication ─────────────────────────────────────────────────────────────

def authenticate(node_id: str, api_key: str) -> Optional[dict]:
    """Return the node row when node_id + api_key are valid, else None."""
    if not node_id or not api_key:
        return None
    row = db.query_one("SELECT * FROM nodes WHERE node_id = %s", (node_id,))
    if row is None or not secrets.compare_digest(row["api_key"], api_key):
        return None
    return row


def rekey_node(node_id: str, recovery_token: str) -> Optional[dict]:
    """Issue a fresh api_key for node_id, proven by its recovery_token instead
    of the (now-dead) api_key. Preserves node_id, and with it every bit of the
    node's history -- unlike falling back to a brand new registration.

    Rotates recovery_token too, so a leaked one-time use doesn't grant
    standing access -- the caller must persist the new one to keep the
    ability to self-recover again later. Returns {"api_key", "recovery_token"},
    or None if the token is wrong or the node never had one (nodes registered
    before this existed)."""
    if not node_id or not recovery_token:
        return None
    row = db.query_one("SELECT * FROM nodes WHERE node_id = %s", (node_id,))
    if (row is None or not row.get("recovery_token")
            or not secrets.compare_digest(row["recovery_token"], recovery_token)):
        return None
    new_api_key = secrets.token_urlsafe(32)
    new_recovery_token = secrets.token_urlsafe(32)
    db.execute(
        "UPDATE nodes SET api_key = %s, recovery_token = %s WHERE node_id = %s",
        (new_api_key, new_recovery_token, node_id),
    )
    logger.info("Node %s recovered via recovery_token — issued fresh api_key", node_id)
    return {"api_key": new_api_key, "recovery_token": new_recovery_token}


# ── Heartbeats ─────────────────────────────────────────────────────────────────

def reissue_api_key(node_id: str) -> Optional[str]:
    """Issue a fresh api_key for node_id on behalf of a member who owns it
    but lost the key -- e.g. the local keychain never persisted it -- and
    has no recovery_token handy either. The caller (an authenticated member
    session) is responsible for verifying ownership before calling this;
    unlike authenticate()/rekey_node() it doesn't itself require proving
    possession of the old key or a recovery_token. Returns the new api_key,
    or None if the node doesn't exist."""
    row = db.query_one("SELECT 1 FROM nodes WHERE node_id = %s", (node_id,))
    if row is None:
        return None
    new_api_key = secrets.token_urlsafe(32)
    db.execute("UPDATE nodes SET api_key = %s WHERE node_id = %s",
               (new_api_key, node_id))
    logger.info("Node %s api_key reissued via member credential repair", node_id)
    return new_api_key


def heartbeat(node_id: str, conditions: Optional[dict] = None) -> None:
    """Record a heartbeat, optionally with current local conditions
    (sky temperature, detected cloud, safety state, utc_offset_hours, ...).

    Does not override disabled/contributor status — those are app-managed states.
    Does not wake a sleeping portable node — the owner starts it explicitly
    for tonight (start_session); merely powering on and heartbeating is not
    enough, otherwise a portable node left running at home would show as
    online without anyone choosing to observe.
    Auto-applies a scheduled vacation once vacation_from arrives, and clears
    it once vacation_until has passed — both are plain ISO date strings, so
    lexicographic comparison against today's date works directly.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    params: list = [_now()]
    sql = (
        "UPDATE nodes SET last_heartbeat = %s, "
        "status = CASE "
        "WHEN status IN ('disabled', 'contributor', 'sleeping') THEN status "
        "WHEN vacation_until <> '' AND vacation_until >= %s "
        "     AND (vacation_from = '' OR vacation_from <= %s) THEN 'vacation' "
        "ELSE 'active' END"
    )
    params.append(today)
    params.append(today)
    # Record first contact once and never overwrite it.
    sql += (", first_heartbeat_at = CASE WHEN COALESCE(first_heartbeat_at, '') = ''"
            " THEN %s ELSE first_heartbeat_at END")
    params.append(_now())
    if not isinstance(conditions, dict):
        conditions = None
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


def update_characterization(node_id: str, report: dict) -> dict:
    """Apply a node's self-measured optics to its capability columns.

    Guarded: at least 10 solved frames behind the medians, and every value
    inside physical bounds. CHORUS and scoring already read these columns,
    so a rig that registered with vague specs converges to reality without
    any scheduler changes. Provenance lands in measured_specs/measured_at.
    """
    try:
        n_frames = int(report.get("n_frames") or 0)
    except (TypeError, ValueError):
        n_frames = 0
    if n_frames < 10:
        return {"ok": False, "error": "need >= 10 solved frames"}

    bounds = {
        "pixel_scale_arcsec": (0.05, 60.0),
        "fov_deg":            (0.02, 30.0),
        "fwhm_arcsec":        (0.3, 60.0),
        "limiting_mag":       (8.0, 22.0),
    }
    accepted = {}
    for key, (lo, hi) in bounds.items():
        val = report.get(key)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if lo <= val <= hi:
            accepted[key] = round(val, 4)

    if not accepted:
        return {"ok": False, "error": "no values within physical bounds"}

    sets, params = [], []
    if "pixel_scale_arcsec" in accepted:
        sets.append("pixel_scale_arcsec = %s")
        params.append(accepted["pixel_scale_arcsec"])
    if "fov_deg" in accepted:
        sets.append("fov_deg = %s")
        params.append(accepted["fov_deg"])
    if "limiting_mag" in accepted:
        sets.append("mag_faint_limit = %s")
        params.append(accepted["limiting_mag"])
    now = _now()
    sets += ["measured_specs = %s", "measured_at = %s"]
    params += [json.dumps({**accepted, "n_frames": n_frames}), now]
    params.append(node_id)
    db.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE node_id = %s",
               tuple(params))
    logger.info("Node %s self-characterized (%d frames): %s",
                node_id, n_frames, accepted)
    return {"ok": True, "applied": accepted}


def list_nodes(active_only: bool = False) -> list:
    rows = db.query("SELECT * FROM nodes ORDER BY registered_at")
    if active_only:
        rows = [r for r in rows if is_online(r)]
    return rows


def is_online(node_row: dict) -> bool:
    """True when the node has heartbeated recently and is not in a non-observing state."""
    status = node_row.get("status", "active")
    if status in ("disabled", "sleeping", "vacation", "contributor"):
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


#: How many nights of the reliability window we need before the observing-rate
#: term means anything. Below this the member was away for almost all of it and
#: there is simply no evidence either way.
_MIN_AVAILABLE_NIGHTS = 5


def opted_out_nights(node_id: str, node_row: Optional[dict],
                     window_days: int = 30) -> set:
    """Dates in the window the member chose not to observe.

    Deliberately member-initiated only: declined nights, stand-downs, and
    vacation. Weather holds are *not* excluded -- a site that is clouded out
    often genuinely delivers less, and that is legitimate information for
    scheduling. What is not legitimate is treating "I am away this week" as
    identical to "my telescope is broken".

    Fails open: any problem reading the intents returns an empty set, so a node
    is scored the way it always was rather than being silently exempted.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=window_days)
    out: set = set()

    try:
        rows = db.query(
            """SELECT night, status FROM night_intents
               WHERE node_id = %s AND night >= %s""",
            (node_id, start.isoformat()),
        )
    except Exception as exc:      # table absent on an un-migrated database
        logger.debug("Could not read night intents for %s: %s", node_id, exc)
        rows = []
    for row in rows:
        if (row.get("status") or "") in ("declined", "stood_down"):
            night = str(row.get("night") or "")
            if night:
                out.add(night)

    # Vacation predates night_intents and is still how a multi-night absence is
    # recorded, so it has to be counted here too or a fortnight away would
    # still read as a fortnight of failure.
    row = node_row or {}
    vac_from = str(row.get("vacation_from") or "").strip()
    vac_until = str(row.get("vacation_until") or "").strip()
    if vac_until:
        try:
            until_d = datetime.fromisoformat(vac_until).date()
            from_d = (datetime.fromisoformat(vac_from).date() if vac_from
                      else until_d - timedelta(days=window_days))
            day = max(from_d, start)
            while day <= min(until_d, today):
                out.add(day.isoformat())
                day += timedelta(days=1)
        except ValueError:
            pass

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
          + 0.20 × (clear_nights_30d / nights_available)
                                                (when it could observe, did it?
                                                 nights the member opted out of
                                                 are not counted against them)
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

    # Nights the member opted out of are removed from the denominator rather
    # than counted as nights the telescope failed to deliver. Scoring them as
    # failures made a holiday indistinguishable from a broken mount, and this
    # number multiplies tile value in event_tiling -- so taking a week off cost
    # a member their share of transient response for the following month.
    node_row = db.query_one(
        "SELECT vacation_from, vacation_until FROM nodes WHERE node_id = %s",
        (node_id,))
    opted_out = opted_out_nights(node_id, node_row)
    available_nights = max(0, 30 - len(opted_out))
    if available_nights < _MIN_AVAILABLE_NIGHTS:
        # Away for essentially the whole window. The question this term asks --
        # "when it could have observed, did it?" -- has no answer, so it must
        # not be answered against them.
        observing_rate = 1.0
    else:
        observing_rate = min(1.0, clear_nights / available_nights)

    if total < 10:
        reliability = 0.5   # insufficient data — neutral score
    else:
        precision_factor = max(0.0, 1.0 - mean_unc / 0.30) if mean_unc > 0 else 0.5
        reliability = (
            0.40 * acceptance_rate
            + 0.25 * (1.0 - outlier_rate)
            + 0.20 * observing_rate
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
    perf = {
        "node_id":               node_id,
        "total_observations":    total,
        "aavso_accepted":        accepted,
        "aavso_rejected":        rejected,
        "mean_uncertainty":      mean_unc,
        "mean_fwhm":             mean_fwhm,
        "clear_nights_30d":      clear_nights,
        "outlier_rate":          outlier_rate,
        "reliability_score":     reliability,
        "scheduler_trust_score": scheduler_trust,
        "incident_penalty":      incident_penalty,
    }
    incidents.auto_triage(node_id, perf)
    return perf


def refresh_all_performance() -> int:
    """Refresh performance stats for every active node.  Returns node count.

    Tier-0 contributor nodes are skipped: they never execute plans, so an
    AAVSO-acceptance-based reliability score would be meaningless noise."""
    nodes = db.query(
        "SELECT node_id FROM nodes WHERE status != 'disabled' AND tier > 0")
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


# ── Vacation management ────────────────────────────────────────────────────────

def set_vacation(node_id: str, until_date: str, from_date: str = "") -> None:
    """Schedule a vacation from *from_date* through *until_date* (ISO 'YYYY-MM-DD').

    *from_date* defaults to today (immediate start) when omitted. A future
    from_date lets a member plan a trip in advance: the node keeps operating
    normally and only flips to 'vacation' once that date arrives (applied on
    the next heartbeat, and independently honored by the network planner's
    date-window check regardless of the status column).

    Missed nights during vacation are excluded from the reliability score.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    from_date = from_date or today
    db.execute(
        "UPDATE nodes SET vacation_from = %s, vacation_until = %s, "
        "status = CASE WHEN %s <= %s AND %s >= %s THEN 'vacation' ELSE status END "
        "WHERE node_id = %s",
        (from_date, until_date, from_date, today, until_date, today, node_id),
    )
    logger.info("Node %s on vacation %s → %s", node_id, from_date, until_date)


def set_dry_run(node_id: str, minutes: float) -> str:
    """Enable admin dry-run testing mode for *minutes* from now.

    While active, the cloud planner ignores actual darkness for this node
    (cloud/network_planner.py::build_node_context) and the node's own hardware
    safety latch ignores actual sun position (alpaca/safety_manager.py), so a
    full night run — real slews, real exposures — can be exercised in
    daylight. Bounded and re-checked every call rather than a standing flag,
    so it can't be left on by accident.
    """
    until = (datetime.now(timezone.utc) + timedelta(minutes=max(1.0, minutes))).isoformat()
    db.execute("UPDATE nodes SET dry_run_until = %s WHERE node_id = %s", (until, node_id))
    logger.warning("Node %s: admin dry-run mode enabled until %s", node_id, until)
    return until


def clear_dry_run(node_id: str) -> None:
    """Disable dry-run mode immediately."""
    db.execute("UPDATE nodes SET dry_run_until = '' WHERE node_id = %s", (node_id,))
    logger.info("Node %s: admin dry-run mode cleared", node_id)


def dry_run_active(node: dict) -> bool:
    """Whether *node*'s dry_run_until timestamp is set and still in the future."""
    until = (node.get("dry_run_until") or "").strip()
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > datetime.now(timezone.utc)
    except ValueError:
        return False


def clear_vacation(node_id: str) -> None:
    """Cancel an active or scheduled vacation.

    If the vacation is already active, portable nodes return to sleeping and
    fixed nodes to offline. If it hasn't started yet (a future from_date),
    the node's current status is left untouched — it was never paused.
    """
    node = db.query_one("SELECT portable, status FROM nodes WHERE node_id = %s", (node_id,))
    if node is None:
        return
    if node.get("status") == "vacation":
        new_status = "sleeping" if node.get("portable") else "offline"
        db.execute(
            "UPDATE nodes SET status = %s, vacation_until = '', vacation_from = '' "
            "WHERE node_id = %s",
            (new_status, node_id),
        )
    else:
        new_status = node.get("status")
        db.execute(
            "UPDATE nodes SET vacation_until = '', vacation_from = '' WHERE node_id = %s",
            (node_id,),
        )
    logger.info("Node %s vacation cleared → %s", node_id, new_status)


def effective_status(node_row: dict) -> str:
    """The node's status as it should be displayed right now.

    The stored `status` column only flips out of 'vacation' as a side effect
    of the next heartbeat (see heartbeat() above), so a node that stops
    heartbeating — offline, unplugged, a portable node left sleeping — can
    show a stale 'vacation until <past date>' indefinitely even though the
    window has closed. Recompute from the date fields directly, the same way
    network_planner.build_node_context() already does for scheduling, so the
    member-facing view never lags behind the calendar.
    """
    status = node_row.get("status", "active")
    if status != "vacation":
        return status
    vac_until = (node_row.get("vacation_until") or "").strip()
    if not vac_until:
        return status
    try:
        today = datetime.now(timezone.utc).date()
        vac_from = (node_row.get("vacation_from") or "").strip()
        from_d = datetime.fromisoformat(vac_from).date() if vac_from else today
        until_d = datetime.fromisoformat(vac_until).date()
    except ValueError:
        return status
    if from_d <= today <= until_d:
        return status
    return "sleeping" if node_row.get("portable") else "active"

