#!/usr/bin/env python3
"""
Environmental conditions for scoring and scheduling.

    fetch_light_pollution(lat, lon, api_key)  → (mpsas, bortle)
    fetch_weather(lat, lon)                   → hourly cloud/humidity forecast
    moon_state(when)                          → illumination, RA/Dec
    sun_alt / target_altaz / airmass_from_alt — astropy wrappers

All network fetchers degrade gracefully: on any failure they log and return a
sensible default so a missing API or offline service never stalls the cloud.
"""

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("cloud.conditions")


# ── Light pollution ────────────────────────────────────────────────────────────

def fetch_light_pollution(lat: float, lon: float,
                          api_key: str = "") -> tuple:
    """
    Fetch sky brightness for a location, returning (mpsas, bortle).

    Tries lightpollutionmap.info's QueryRaster API (VIIRS 2022 artificial
    radiance) when an API key is configured; otherwise falls back to a default
    suburban sky of 20.0 mag/arcsec² (Bortle 5).
    """
    if api_key:
        try:
            import requests
            resp = requests.get(
                "https://www.lightpollutionmap.info/QueryRaster/",
                params={"ql": "viirs_2022", "qt": "point",
                        "qd": f"{lon},{lat}", "key": api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                radiance = float(resp.text.strip().split(";")[0])
                mpsas = _radiance_to_mpsas(radiance)
                bortle = mpsas_to_bortle(mpsas)
                logger.info("Light pollution at %.3f,%.3f: %.2f mpsas (Bortle %d)",
                            lat, lon, mpsas, bortle)
                return mpsas, bortle
            logger.warning("Light pollution API returned HTTP %d", resp.status_code)
        except Exception as exc:
            logger.warning("Light pollution fetch failed: %s", exc)

    logger.info("Light pollution defaulting to 20.0 mpsas (Bortle 5) for %.3f,%.3f",
                lat, lon)
    return 20.0, 5


def _radiance_to_mpsas(radiance: float) -> float:
    """
    Convert VIIRS artificial radiance (1e-9 W/cm²·sr) to total sky brightness
    in mag/arcsec², adding the natural sky background (~0.171 mcd/m²).

    Standard conversion used by lightpollutionmap.info / Falchi et al. 2016.
    """
    artificial_mcd = max(radiance, 0.0) * 0.163   # radiance → luminance mcd/m²
    total_mcd = artificial_mcd + 0.171
    return float(math.log10(total_mcd / 108_000_000.0) / -0.4)


def mpsas_to_bortle(mpsas: float) -> int:
    """Map sky brightness (mag/arcsec²) to the Bortle scale (1=darkest, 9=inner city)."""
    scale = [
        (21.99, 1), (21.89, 2), (21.69, 3), (20.49, 4),
        (19.50, 5), (18.94, 6), (18.38, 7), (17.80, 8),
    ]
    for limit, bortle in scale:
        if mpsas >= limit:
            return bortle
    return 9


# ── Weather (Open-Meteo, free, no API key) ─────────────────────────────────────

_weather_cache: dict = {}   # (lat,lon rounded) → (fetched_monotonic, forecast)
_WEATHER_TTL_S = 1800


def fetch_weather(lat: float, lon: float) -> Optional[dict]:
    """
    Hourly forecast for the next 48 h:
        {"times": [iso, ...], "cloud_cover": [%], "humidity": [%], "wind_kmh": [...]}

    Cached for 30 minutes per location. Returns None when unavailable.
    """
    key = (round(lat, 2), round(lon, 2))
    cached = _weather_cache.get(key)
    if cached and time.monotonic() - cached[0] < _WEATHER_TTL_S:
        return cached[1]

    try:
        import requests
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "cloud_cover,relative_humidity_2m,wind_speed_10m",
                "forecast_days": 2, "timezone": "UTC",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Open-Meteo returned HTTP %d", resp.status_code)
            return cached[1] if cached else None
        hourly = resp.json().get("hourly", {})
        forecast = {
            "times":       hourly.get("time", []),
            "cloud_cover": hourly.get("cloud_cover", []),
            "humidity":    hourly.get("relative_humidity_2m", []),
            "wind_kmh":    hourly.get("wind_speed_10m", []),
        }
        _weather_cache[key] = (time.monotonic(), forecast)
        return forecast
    except Exception as exc:
        logger.warning("Weather fetch failed for %.2f,%.2f: %s", lat, lon, exc)
        return cached[1] if cached else None


def cloud_cover_at(forecast: Optional[dict], when_utc: datetime) -> Optional[float]:
    """Cloud cover fraction 0..1 at the forecast hour nearest `when_utc`."""
    if not forecast or not forecast.get("times"):
        return None
    target = when_utc.replace(tzinfo=None)
    best_i, best_dt = None, None
    for i, t in enumerate(forecast["times"]):
        try:
            ft = datetime.fromisoformat(t)
        except ValueError:
            continue
        d = abs((ft - target).total_seconds())
        if best_dt is None or d < best_dt:
            best_i, best_dt = i, d
    if best_i is None or best_dt is None or best_dt > 7200:
        return None
    try:
        return float(forecast["cloud_cover"][best_i]) / 100.0
    except (IndexError, TypeError, ValueError):
        return None


# ── Astronomy helpers (astropy) ────────────────────────────────────────────────

def sun_alt(lat: float, lon: float, when_utc: datetime) -> float:
    """Sun altitude in degrees at a location/time."""
    from astropy.coordinates import AltAz, EarthLocation, get_sun
    from astropy.time import Time
    import astropy.units as u

    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)
    t = Time(when_utc)
    return float(get_sun(t).transform_to(AltAz(obstime=t, location=loc)).alt.deg)


def target_alt(ra_deg: float, dec_deg: float,
               lat: float, lon: float, when_utc: datetime) -> float:
    """Target altitude in degrees."""
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    import astropy.units as u

    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)
    t = Time(when_utc)
    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    return float(coord.transform_to(AltAz(obstime=t, location=loc)).alt.deg)


def airmass_from_alt(alt_deg: float) -> float:
    """Secant airmass with a hard floor at 5° altitude."""
    if alt_deg <= 5.0:
        return 11.5
    return 1.0 / math.cos(math.radians(90.0 - alt_deg))


def moon_state(when_utc: datetime) -> dict:
    """
    Moon illumination fraction (0..1) and geocentric RA/Dec at a moment.
        {"illumination": 0.42, "ra_deg": ..., "dec_deg": ...}
    """
    from astropy.coordinates import get_body, get_sun
    from astropy.time import Time

    t = Time(when_utc)
    moon = get_body("moon", t)
    sun = get_sun(t)
    elongation = float(sun.separation(moon).deg)
    illumination = (1.0 - math.cos(math.radians(elongation))) / 2.0
    return {
        "illumination": illumination,
        "ra_deg":  float(moon.ra.deg),
        "dec_deg": float(moon.dec.deg),
    }


def angular_separation_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle separation between two sky positions, in degrees."""
    ra1, dec1, ra2, dec2 = map(math.radians, (ra1, dec1, ra2, dec2))
    cos_sep = (math.sin(dec1) * math.sin(dec2)
               + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


def night_window(lat: float, lon: float,
                 start_utc: Optional[datetime] = None,
                 sun_limit_deg: float = -12.0,
                 step_min: int = 10) -> Optional[tuple]:
    """
    Find the next astronomical night (sun below `sun_limit_deg`) within 24 h
    of `start_utc`. Returns (night_start_utc, night_end_utc) or None (polar
    day / no darkness).

    Vectorised over astropy to keep this fast enough to run per node.
    """
    from astropy.coordinates import AltAz, EarthLocation, get_sun
    from astropy.time import Time, TimeDelta
    import astropy.units as u
    import numpy as np

    if start_utc is None:
        start_utc = datetime.now(timezone.utc)

    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)
    n = int(24 * 60 / step_min) + 1
    times = Time(start_utc) + TimeDelta(np.arange(n) * step_min * 60, format="sec")
    alts = get_sun(times).transform_to(AltAz(obstime=times, location=loc)).alt.deg

    dark = alts < sun_limit_deg
    if not dark.any():
        return None

    # First dark sample, then the end of that contiguous dark stretch
    i0 = int(np.argmax(dark))
    i1 = i0
    while i1 + 1 < n and dark[i1 + 1]:
        i1 += 1

    t0 = start_utc + timedelta(minutes=i0 * step_min)
    t1 = start_utc + timedelta(minutes=i1 * step_min)
    if t1 <= t0:
        return None
    return t0, t1


def altitude_curve(ra_deg: float, dec_deg: float, lat: float, lon: float,
                   t_start: datetime, t_end: datetime,
                   step_min: int = 10) -> list:
    """
    Sample target altitude between two times.
    Returns [(datetime_utc, alt_deg), ...]. Vectorised.
    """
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time, TimeDelta
    import astropy.units as u
    import numpy as np

    n = max(2, int((t_end - t_start).total_seconds() / 60 / step_min) + 1)
    loc = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)
    times = Time(t_start) + TimeDelta(np.arange(n) * step_min * 60, format="sec")
    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    alts = coord.transform_to(AltAz(obstime=times, location=loc)).alt.deg

    return [(t_start + timedelta(minutes=i * step_min), float(alts[i]))
            for i in range(n)]
