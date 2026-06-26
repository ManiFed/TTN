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

_lp_cache: dict = {}          # (lat,lon rounded) → (fetched_monotonic, mpsas, bortle, source)
_LP_TTL_S = 7 * 24 * 3600    # light pollution changes on timescales of months; 1-week cache is fine


def fetch_light_pollution(lat: float, lon: float, api_key: str = "") -> tuple:
    """
    Fetch sky brightness for a location, returning (mpsas, bortle).

    Source priority (first success wins):
      1. lightpollutionmap.info QueryRaster with API key (VIIRS 2022 radiance)
      2. lightpollutionmap.info QueryRaster without key (public rate-limited tier)
      3. NASA EOG / VIIRS annual composite via Colorado School of Mines WCS
      4. Default: 20.0 mpsas (Bortle 5) — suburban sky

    Results are cached for 7 days per location.  Call
    fetch_light_pollution_detail() for the full source-annotated result.
    """
    result = fetch_light_pollution_detail(lat, lon, api_key)
    return result["mpsas"], result["bortle"]


def fetch_light_pollution_detail(lat: float, lon: float, api_key: str = "") -> dict:
    """
    Full light-pollution result dict:
        {"mpsas": float, "bortle": int, "source": str, "radiance": float|None}

    Cached 7 days per (lat, lon) rounded to 0.05°.
    """
    key = (round(lat * 20) / 20, round(lon * 20) / 20)   # 0.05° grid
    cached = _lp_cache.get(key)
    if cached and time.monotonic() - cached[0] < _LP_TTL_S:
        return {"mpsas": cached[1], "bortle": cached[2], "source": cached[3], "radiance": cached[4]}

    try:
        import requests
    except ImportError:
        logger.warning("requests not installed — cannot fetch light pollution")
        return _lp_default(lat, lon)

    # ── Source 1: lightpollutionmap.info with API key ──────────────────────────
    if api_key:
        result = _fetch_lpm_info(lat, lon, api_key, requests)
        if result:
            _lp_cache[key] = (time.monotonic(), result["mpsas"], result["bortle"],
                              result["source"], result.get("radiance"))
            return result

    # ── Source 2: lightpollutionmap.info without key (public tier) ────────────
    result = _fetch_lpm_info(lat, lon, "", requests)
    if result:
        _lp_cache[key] = (time.monotonic(), result["mpsas"], result["bortle"],
                          result["source"], result.get("radiance"))
        return result

    # ── Source 3: NASA EOG VIIRS annual composite (Colorado School of Mines) ───
    result = _fetch_eog_viirs(lat, lon, requests)
    if result:
        _lp_cache[key] = (time.monotonic(), result["mpsas"], result["bortle"],
                          result["source"], result.get("radiance"))
        return result

    # ── Source 4: default ──────────────────────────────────────────────────────
    return _lp_default(lat, lon)


def _fetch_lpm_info(lat: float, lon: float, api_key: str, requests) -> Optional[dict]:
    """lightpollutionmap.info QueryRaster — works with or without key."""
    params = {"ql": "viirs_2022", "qt": "point", "qd": f"{lon},{lat}"}
    if api_key:
        params["key"] = api_key
    try:
        resp = requests.get(
            "https://www.lightpollutionmap.info/QueryRaster/",
            params=params, timeout=15,
        )
        if resp.status_code == 200:
            radiance = float(resp.text.strip().split(";")[0])
            mpsas = _radiance_to_mpsas(radiance)
            bortle = mpsas_to_bortle(mpsas)
            source = "lightpollutionmap.info (keyed)" if api_key else "lightpollutionmap.info (public)"
            logger.info("LP %s at %.3f,%.3f: %.2f mpsas (Bortle %d) [%s]",
                        "keyed" if api_key else "public", lat, lon, mpsas, bortle, source)
            return {"mpsas": mpsas, "bortle": bortle, "source": source, "radiance": radiance}
        logger.debug("lightpollutionmap.info returned HTTP %d%s",
                     resp.status_code, " (key required?)" if resp.status_code == 401 else "")
    except Exception as exc:
        logger.debug("lightpollutionmap.info fetch failed: %s", exc)
    return None


def _fetch_eog_viirs(lat: float, lon: float, requests) -> Optional[dict]:
    """
    NASA Earth Observation Group VIIRS Nighttime Lights annual composite.
    Uses the public WCS endpoint at Colorado School of Mines EOG.
    Returns radiance in nW/cm²/sr → converts via same Falchi formula.
    """
    # EOG WCS for VNL (VIIRS Nighttime Lights) annual composite
    # Bounding box: tiny box around the point (0.01° each side)
    bbox = f"{lon-0.01},{lat-0.01},{lon+0.01},{lat+0.01}"
    try:
        resp = requests.get(
            "https://eogdata.mines.edu/geoserver/VIIRS/wcs",
            params={
                "SERVICE": "WCS",
                "VERSION": "1.0.0",
                "REQUEST": "GetCoverage",
                "COVERAGE": "VNL_v2_npp_2023_global_vcmslnl_c202402081600",
                "CRS": "EPSG:4326",
                "BBOX": bbox,
                "WIDTH": "1",
                "HEIGHT": "1",
                "FORMAT": "GeoTIFF",
            },
            timeout=20,
        )
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
            radiance = _extract_geotiff_value(resp.content)
            if radiance is not None and radiance >= 0:
                mpsas = _radiance_to_mpsas(radiance)
                bortle = mpsas_to_bortle(mpsas)
                logger.info("LP EOG/VIIRS at %.3f,%.3f: %.3f nW → %.2f mpsas (Bortle %d)",
                            lat, lon, radiance, mpsas, bortle)
                return {"mpsas": mpsas, "bortle": bortle,
                        "source": "NASA EOG VIIRS 2023", "radiance": radiance}
        logger.debug("EOG VIIRS returned HTTP %d / content-type %s",
                     resp.status_code, resp.headers.get("content-type", "?"))
    except Exception as exc:
        logger.debug("EOG VIIRS fetch failed: %s", exc)
    return None


def _extract_geotiff_value(data: bytes) -> Optional[float]:
    """Read the first pixel value from a single-band GeoTIFF byte string."""
    try:
        import struct
        # Minimal TIFF reader: find StripOffsets (tag 278) and read one float/int32
        if data[:2] not in (b"II", b"MM"):
            return None
        little = data[:2] == b"II"
        bo = "<" if little else ">"
        ifd_offset = struct.unpack_from(f"{bo}I", data, 4)[0]
        n_entries = struct.unpack_from(f"{bo}H", data, ifd_offset)[0]
        tags = {}
        for i in range(n_entries):
            base = ifd_offset + 2 + i * 12
            tag, typ, count, val_off = struct.unpack_from(f"{bo}HHII", data, base)
            tags[tag] = (typ, count, val_off)
        # StripOffsets=273, SampleFormat=339, BitsPerSample=258
        strip_tag = tags.get(273)
        if strip_tag is None:
            return None
        strip_offset = strip_tag[2]
        bits_tag = tags.get(258)
        bits = bits_tag[2] if bits_tag and bits_tag[1] == 1 else 32
        fmt_tag = tags.get(339)
        sample_fmt = fmt_tag[2] if fmt_tag and fmt_tag[1] == 1 else 1
        # sample_fmt: 1=uint, 2=int, 3=float
        fmt_map = {(32, 3): f"{bo}f", (32, 1): f"{bo}I", (32, 2): f"{bo}i",
                   (16, 1): f"{bo}H", (16, 2): f"{bo}h"}
        fmt = fmt_map.get((bits, sample_fmt), f"{bo}f")
        val = struct.unpack_from(fmt, data, strip_offset)[0]
        return float(val)
    except Exception:
        return None


def _lp_default(lat: float, lon: float) -> dict:
    logger.info("Light pollution defaulting to 20.0 mpsas (Bortle 5) for %.3f,%.3f", lat, lon)
    return {"mpsas": 20.0, "bortle": 5, "source": "default", "radiance": None}


def _radiance_to_mpsas(radiance: float) -> float:
    """
    Convert VIIRS artificial radiance (nW/cm²/sr) to total sky brightness
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


# ── Astronomy weather (7timer ASTRO — seeing, transparency, cloud cover) ──────

_astro_wx_cache: dict = {}   # (lat,lon rounded) → (fetched_monotonic, forecast)
_ASTRO_WX_TTL_S = 3600       # 7timer updates every 8 h; hourly polling is wasteful

# 7timer seeing scale: 1=<0.5" (excellent) … 8=>4" (terrible) → invert to 0..1
_SEEING_SCORE = {1: 1.00, 2: 0.90, 3: 0.75, 4: 0.60,
                 5: 0.45, 6: 0.30, 7: 0.15, 8: 0.05}
# 7timer transparency scale: 1=<0.3 mag (excellent) … 8=>1 mag (bad) → invert
_TRANSP_SCORE = {1: 1.00, 2: 0.85, 3: 0.70, 4: 0.55,
                 5: 0.40, 6: 0.25, 7: 0.15, 8: 0.05}


def fetch_astronomy_weather(lat: float, lon: float) -> Optional[dict]:
    """
    3-day astronomy forecast from 7timer ASTRO (free, no API key).

    Returns:
        {
            "times":        [datetime_utc, ...],   # one per 3-h slot
            "cloud_cover":  [0..100, ...],         # %
            "seeing":       [1..8, ...],           # 1=best
            "transparency": [1..8, ...],           # 1=best
            "lifted_index": [float, ...],          # >0 = stable
            "wind_kmh":     [float, ...],
            "humidity":     [0..100, ...],
        }
    Returns None on any fetch failure.
    Cached for 1 hour per location.
    """
    key = (round(lat, 2), round(lon, 2))
    cached = _astro_wx_cache.get(key)
    if cached and time.monotonic() - cached[0] < _ASTRO_WX_TTL_S:
        return cached[1]

    try:
        import requests
        resp = requests.get(
            "http://www.7timer.info/bin/api.pl",
            params={"lon": lon, "lat": lat, "product": "astro",
                    "output": "json", "unit": "metric"},
            timeout=20,
        )
        if resp.status_code != 200:
            logger.warning("7timer returned HTTP %d for %.2f,%.2f",
                           resp.status_code, lat, lon)
            return cached[1] if cached else None

        data = resp.json()
        init_str = data.get("init", "")          # e.g. "2024062612"
        dataseries = data.get("dataseries", [])
        if not dataseries:
            logger.warning("7timer returned empty dataseries for %.2f,%.2f", lat, lon)
            return cached[1] if cached else None

        try:
            init_dt = datetime.strptime(init_str, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("7timer unparseable init time: %s", init_str)
            return cached[1] if cached else None

        times, cloud, seeing, transp, li, wind, humidity = [], [], [], [], [], [], []
        for entry in dataseries:
            offset_h = int(entry.get("timepoint", 0))
            times.append(init_dt + timedelta(hours=offset_h))
            cloud.append(int(entry.get("cloudcover", 9)))   # 1..9 in ASTRO, scale differs
            seeing.append(int(entry.get("seeing", 5)))
            transp.append(int(entry.get("transparency", 5)))
            li.append(float(entry.get("lifted_index", 0)))
            w = entry.get("wind10m", {})
            wind.append(float(w.get("speed", 0)) * 1.852 if isinstance(w, dict) else 0.0)
            humidity.append(float(entry.get("rh2m", 50)))

        # 7timer cloudcover: 1=0-6%, 2=6-19%, … 9=94-100%
        # Map to midpoint percentages
        _cloud_mid = {1: 3, 2: 13, 3: 25, 4: 44, 5: 56, 6: 69, 7: 75, 8: 88, 9: 97}
        cloud_pct = [_cloud_mid.get(c, 50) for c in cloud]

        forecast = {
            "times":        times,
            "cloud_cover":  cloud_pct,
            "seeing":       seeing,
            "transparency": transp,
            "lifted_index": li,
            "wind_kmh":     wind,
            "humidity":     humidity,
        }
        _astro_wx_cache[key] = (time.monotonic(), forecast)
        logger.info("7timer ASTRO fetched for %.2f,%.2f: %d slots", lat, lon, len(times))
        return forecast

    except Exception as exc:
        logger.warning("7timer fetch failed for %.2f,%.2f: %s", lat, lon, exc)
        return cached[1] if cached else None


def _nearest_astro_index(forecast: dict, when_utc: datetime) -> Optional[int]:
    """Index of the 7timer slot nearest to when_utc (within 2 h)."""
    target = when_utc.replace(tzinfo=timezone.utc) if when_utc.tzinfo is None else when_utc
    best_i, best_dt = None, None
    for i, t in enumerate(forecast["times"]):
        d = abs((t - target).total_seconds())
        if best_dt is None or d < best_dt:
            best_i, best_dt = i, d
    if best_i is None or best_dt > 7200:
        return None
    return best_i


def seeing_score_at(forecast: Optional[dict], when_utc: datetime) -> Optional[float]:
    """Normalised seeing quality 0..1 (1 = best) at the nearest forecast hour."""
    if not forecast or not forecast.get("seeing"):
        return None
    i = _nearest_astro_index(forecast, when_utc)
    if i is None:
        return None
    return _SEEING_SCORE.get(forecast["seeing"][i], 0.5)


def transparency_score_at(forecast: Optional[dict], when_utc: datetime) -> Optional[float]:
    """Normalised transparency quality 0..1 (1 = best) at the nearest forecast hour."""
    if not forecast or not forecast.get("transparency"):
        return None
    i = _nearest_astro_index(forecast, when_utc)
    if i is None:
        return None
    return _TRANSP_SCORE.get(forecast["transparency"][i], 0.5)


def astro_cloud_cover_at(forecast: Optional[dict], when_utc: datetime) -> Optional[float]:
    """Cloud cover fraction 0..1 from 7timer ASTRO at the nearest slot."""
    if not forecast or not forecast.get("cloud_cover"):
        return None
    i = _nearest_astro_index(forecast, when_utc)
    if i is None:
        return None
    try:
        return float(forecast["cloud_cover"][i]) / 100.0
    except (IndexError, TypeError, ValueError):
        return None


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
