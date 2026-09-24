#!/usr/bin/env python3
"""
External observation coverage — queries public astronomical APIs to estimate
how well the global community is observing each target, independent of our
node network.

Used by scoring.historical_neglect() to avoid the snowball effect where a
target scores as neglected simply because our nodes were clouded out.

Sources:
  AAVSO  — for variable stars (CV, EB, VAR, AGN, YSO, NOVA)
  ALeRCE — for ZTF transients (SN, TDE, GRB, unknown)

Results are cached in memory with a 6-hour TTL so scoring runs don't hammer
external APIs across hundreds of (target, node) pairs.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("cloud.external_coverage")

# Per-type expected observation rates from external networks over 30 days.
# Used to normalize raw counts into a 0..1 neglect fraction.
# AAVSO: active programs get ~1 obs/day from the community.
# ALeRCE/ZTF: surveys every ~3 nights = ~10 detections/30 days per filter.
_EXPECTED_BY_SOURCE = {
    "aavso":  30.0,
    "alerce": 10.0,
}

# In-memory cache: (target_name, window_days) -> (monotonic_time_s, count_or_none)
_cache: dict[tuple[str, int], tuple[float, Optional[int]]] = {}
_CACHE_TTL_S = 6 * 3600


def _jd(dt: datetime) -> float:
    return (dt - datetime(1858, 11, 17, tzinfo=timezone.utc)).total_seconds() / 86400.0 + 2400000.5


def _http_get_json(url: str, params: dict = None, timeout: int = 20):
    import requests
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ── Per-source fetchers ────────────────────────────────────────────────────────

def _aavso_count(name: str, window_days: int) -> Optional[int]:
    """Observations reported to AAVSO for this star in the last window_days."""
    now = datetime.now(timezone.utc)
    jd_end = _jd(now)
    jd_start = _jd(now - timedelta(days=window_days))
    try:
        payload = _http_get_json(
            "https://www.aavso.org/api/v2/observations/",
            params={
                "star_name": name,
                "fromjd": round(jd_start, 2),
                "tojd": round(jd_end, 2),
                "format": "json",
            },
        )
        if isinstance(payload, dict) and "count" in payload:
            return int(payload["count"])
        results = payload.get("results") if isinstance(payload, dict) else payload
        return len(results) if isinstance(results, list) else None
    except Exception as exc:
        logger.debug("AAVSO count failed for %s: %s", name, exc)
        return None


def _alerce_count(ra_deg: float, dec_deg: float, window_days: int) -> Optional[int]:
    """ZTF detections near this position via ALeRCE, scaled to the window.

    ALeRCE doesn't support date-windowed queries on the object-list endpoint,
    so we take ndet (total detections since ZTF start, ~2400 days) and scale
    proportionally.
    """
    try:
        payload = _http_get_json(
            "https://api.alerce.online/ztf/v1/objects/",
            params={
                "ra": ra_deg, "dec": dec_deg,
                "radius": 5.0,
                "order_by": "ndet", "order_mode": "DESC",
                "page_size": 1,
            },
        )
        items = payload.get("items", [])
        if not items:
            return 0
        ndet = int(items[0].get("ndet", 0))
        ztf_lifespan_days = 2400.0
        return max(0, round(ndet * window_days / ztf_lifespan_days))
    except Exception as exc:
        logger.debug("ALeRCE position query failed (%.4f, %.4f): %s",
                     ra_deg, dec_deg, exc)
        return None


# ── Public API ─────────────────────────────────────────────────────────────────

def external_observation_count(target: dict, window_days: int = 30) -> Optional[int]:
    """Estimated external observation count for this target over window_days.

    Returns None when no external source is applicable or all requests fail.
    Results are cached for 6 hours.
    """
    name = target["name"]
    cache_key = (name, window_days)
    now_mono = time.monotonic()

    if cache_key in _cache:
        fetched_at, cached = _cache[cache_key]
        if now_mono - fetched_at < _CACHE_TTL_S:
            return cached

    ttype = target.get("target_type", "unknown")
    count = None

    if ttype in ("CV", "EB", "VAR", "AGN", "YSO", "NOVA"):
        count = _aavso_count(name, window_days)

    if count is None and ttype in ("SN", "TDE", "GRB", "unknown"):
        count = _alerce_count(
            float(target.get("ra_deg", 0)),
            float(target.get("dec_deg", 0)),
            window_days,
        )

    _cache[cache_key] = (now_mono, count)
    return count


def external_neglect(target: dict, window_days: int = 30) -> Optional[float]:
    """0.0 = well-covered globally; 1.0 = genuinely neglected by the community.

    Returns None when no external data is available for this target type.
    """
    count = external_observation_count(target, window_days)
    if count is None:
        return None

    ttype = target.get("target_type", "unknown")
    if ttype in ("CV", "EB", "VAR", "AGN", "YSO", "NOVA"):
        expected = _EXPECTED_BY_SOURCE["aavso"] * (window_days / 30.0)
    else:
        expected = _EXPECTED_BY_SOURCE["alerce"] * (window_days / 30.0)

    return max(0.0, min(1.0, 1.0 - count / max(1.0, expected)))
