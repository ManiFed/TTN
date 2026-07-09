#!/usr/bin/env python3
"""
Auto-detect user location via IP geolocation.
Falls back gracefully if detection fails or is disabled.
"""

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("geolocation")


def detect_location() -> Optional[Dict[str, float]]:
    """
    Detect user location from IP address.

    Returns dict with 'latitude' and 'longitude' keys, or None on failure.
    Uses ip-api.com free tier (no API key required, 45 req/min limit).
    """
    try:
        import requests
    except ImportError:
        logger.warning("requests not installed — cannot auto-detect location")
        return None

    try:
        # Free IP geolocation service, no API key needed
        resp = requests.get("http://ip-api.com/json/", timeout=5)
        if resp.status_code != 200:
            logger.debug("IP geolocation returned HTTP %d", resp.status_code)
            return None

        data = resp.json()
        if data.get("status") != "success":
            logger.debug("IP geolocation failed: %s", data.get("message", "unknown error"))
            return None

        lat = float(data.get("lat"))
        lon = float(data.get("lon"))
        city = data.get("city", "Unknown")
        country = data.get("country", "")

        logger.info(f"Auto-detected location: {city}, {country} ({lat:.4f}°, {lon:.4f}°)")
        return {"latitude": lat, "longitude": lon}

    except Exception as exc:
        logger.debug("Location auto-detection failed: %s", exc)
        return None


def enrich_config_with_location(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    If observatory location is not configured, auto-detect and add it.

    Priority:
      1. Explicit config values (if latitude and longitude are set)
      2. Auto-detect from IP
      3. Leave as-is (null/0.0) if detection fails
    """
    if config is None:
        config = {}

    obs_cfg = config.get("observatory", {})
    safety_obs = config.get("safety", {}).get("observer", {})

    # Check if location is already configured (either path wins)
    lat = obs_cfg.get("latitude") or safety_obs.get("latitude")
    lon = obs_cfg.get("longitude") or safety_obs.get("longitude")
    has_lat = lat is not None and lat != 0.0
    has_lon = lon is not None and lon != 0.0

    if has_lat and has_lon:
        logger.debug("Observatory location already configured")
        return config

    # Try to auto-detect
    location = detect_location()
    if location:
        if "observatory" not in config:
            config["observatory"] = {}
        config["observatory"]["latitude"] = location["latitude"]
        config["observatory"]["longitude"] = location["longitude"]

        # Also update safety.observer for airmass calculations
        if "safety" not in config:
            config["safety"] = {}
        if "observer" not in config["safety"]:
            config["safety"]["observer"] = {}
        config["safety"]["observer"]["latitude"] = location["latitude"]
        config["safety"]["observer"]["longitude"] = location["longitude"]

        logger.info("Updated config with auto-detected location")

    return config
