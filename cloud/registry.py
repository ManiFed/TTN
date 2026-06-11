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
from datetime import datetime, timezone
from typing import Optional

from cloud import db
from cloud.conditions import fetch_light_pollution
from shared_models import NodeInfo

logger = logging.getLogger("cloud.registry")

# How long after the last heartbeat a node is still considered online
HEARTBEAT_STALE_S = 900


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Registration ───────────────────────────────────────────────────────────────

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
        existing = db.query_one("SELECT * FROM nodes WHERE node_id = ?", (node.node_id,))
        if existing and existing["api_key"] != info.get("api_key", ""):
            raise ValueError("node_id already registered with a different API key")

    if existing:
        node_id, api_key = existing["node_id"], existing["api_key"]
    else:
        node_id = node.node_id or f"node_{secrets.token_hex(4)}"
        api_key = secrets.token_urlsafe(32)

    mpsas, bortle = fetch_light_pollution(node.latitude, node.longitude, lp_api_key)

    db.execute(
        """INSERT INTO nodes (node_id, api_key, owner_name, owner_email,
               latitude, longitude, elevation, city, country, utc_offset_hours,
               telescope_model, aperture_mm, focal_length_mm, fov_deg,
               pixel_scale_arcsec, filters, mag_bright_limit, mag_faint_limit,
               min_altitude_deg, max_exposure_s, light_pollution_mpsas, bortle,
               status, registered_at, last_heartbeat)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(node_id) DO UPDATE SET
               owner_name=excluded.owner_name, owner_email=excluded.owner_email,
               latitude=excluded.latitude, longitude=excluded.longitude,
               elevation=excluded.elevation, city=excluded.city,
               country=excluded.country, utc_offset_hours=excluded.utc_offset_hours,
               telescope_model=excluded.telescope_model,
               aperture_mm=excluded.aperture_mm,
               focal_length_mm=excluded.focal_length_mm, fov_deg=excluded.fov_deg,
               pixel_scale_arcsec=excluded.pixel_scale_arcsec,
               filters=excluded.filters,
               mag_bright_limit=excluded.mag_bright_limit,
               mag_faint_limit=excluded.mag_faint_limit,
               min_altitude_deg=excluded.min_altitude_deg,
               max_exposure_s=excluded.max_exposure_s,
               light_pollution_mpsas=excluded.light_pollution_mpsas,
               bortle=excluded.bortle,
               status='active', last_heartbeat=excluded.last_heartbeat""",
        (node_id, api_key, node.owner_name, node.owner_email,
         node.latitude, node.longitude, node.elevation, node.city, node.country,
         node.utc_offset_hours, node.telescope_model, node.aperture_mm,
         node.focal_length_mm, node.fov_deg, node.pixel_scale_arcsec,
         node.filters, node.mag_bright_limit, node.mag_faint_limit,
         node.min_altitude_deg, node.max_exposure_s, mpsas, bortle,
         "active", _now(), _now()),
    )
    logger.info("Node %s %s: %s @ %.4f,%.4f  (%.1f mpsas, Bortle %d)",
                node_id, "updated" if existing else "registered",
                node.telescope_model, node.latitude, node.longitude, mpsas, bortle)
    return {"node_id": node_id, "api_key": api_key}


# ── Authentication ─────────────────────────────────────────────────────────────

def authenticate(node_id: str, api_key: str) -> Optional[dict]:
    """Return the node row when node_id + api_key are valid, else None."""
    if not node_id or not api_key:
        return None
    row = db.query_one("SELECT * FROM nodes WHERE node_id = ?", (node_id,))
    if row is None or not secrets.compare_digest(row["api_key"], api_key):
        return None
    return row


# ── Heartbeats ─────────────────────────────────────────────────────────────────

def heartbeat(node_id: str, conditions: Optional[dict] = None) -> None:
    """Record a heartbeat, optionally with current local conditions
    (sky temperature, detected cloud, safety state, utc_offset_hours, ...)."""
    params: list = [_now()]
    sql = "UPDATE nodes SET last_heartbeat = ?, status = 'active'"
    if conditions:
        sql += ", last_conditions = ?"
        params.append(json.dumps(conditions))
        offset = conditions.get("utc_offset_hours")
        if isinstance(offset, (int, float)) and -14.0 <= offset <= 14.0:
            sql += ", utc_offset_hours = ?"
            params.append(float(offset))
    sql += " WHERE node_id = ?"
    params.append(node_id)
    db.execute(sql, tuple(params))


def get_node(node_id: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM nodes WHERE node_id = ?", (node_id,))


def list_nodes(active_only: bool = False) -> list:
    rows = db.query("SELECT * FROM nodes ORDER BY registered_at")
    if active_only:
        rows = [r for r in rows if is_online(r)]
    return rows


def is_online(node_row: dict) -> bool:
    """True when the node has heartbeated recently and is not disabled."""
    if node_row.get("status") == "disabled":
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
    out["last_conditions"] = db.loads(node_row.get("last_conditions"), {})
    return out


def refresh_light_pollution(lp_api_key: str = "") -> None:
    """Periodic re-fetch of light pollution for every node (it drifts slowly;
    monthly is plenty). Called by the maintenance loop."""
    for row in db.query("SELECT node_id, latitude, longitude FROM nodes"):
        mpsas, bortle = fetch_light_pollution(row["latitude"], row["longitude"], lp_api_key)
        db.execute(
            "UPDATE nodes SET light_pollution_mpsas = ?, bortle = ? WHERE node_id = ?",
            (mpsas, bortle, row["node_id"]),
        )
