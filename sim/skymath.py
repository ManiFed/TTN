#!/usr/bin/env python3
"""
Pure-math astronomy for the digital twin — no astropy, no network, no DB.

The production cloud uses astropy for alt/az curves and night windows
(cloud.conditions).  The twin re-derives the same quantities with standard
low-precision formulas (Meeus-style), accurate to a fraction of a degree —
far inside the tolerance a scheduling simulation needs — and roughly three
orders of magnitude faster, which is what makes 1,000-node scenarios
tractable in pure Python.

Everything is a deterministic function of its arguments.  All datetimes are
aware UTC.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

J2000 = datetime(2000, 1, 1, 12, 0, tzinfo=timezone.utc)


def days_since_j2000(when: datetime) -> float:
    return (when - J2000).total_seconds() / 86400.0


# ── Sidereal time and alt/az ──────────────────────────────────────────────────

def gmst_deg(when: datetime) -> float:
    """Greenwich mean sidereal time in degrees (low-precision, <0.01° err)."""
    d = days_since_j2000(when)
    return (280.46061837 + 360.98564736629 * d) % 360.0


def lst_deg(when: datetime, lon_deg: float) -> float:
    """Local sidereal time in degrees at east-positive longitude `lon_deg`."""
    return (gmst_deg(when) + lon_deg) % 360.0


def altaz(ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float,
          when: datetime) -> tuple:
    """(altitude, azimuth) in degrees; azimuth from North through East —
    same convention as astropy's AltAz used by cloud.conditions."""
    ha = math.radians((lst_deg(when, lon_deg) - ra_deg) % 360.0)
    dec = math.radians(dec_deg)
    lat = math.radians(lat_deg)
    sin_alt = (math.sin(dec) * math.sin(lat)
               + math.cos(dec) * math.cos(lat) * math.cos(ha))
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.asin(sin_alt)
    cos_az = ((math.sin(dec) - math.sin(alt) * math.sin(lat))
              / max(math.cos(alt) * math.cos(lat), 1e-9))
    az = math.acos(max(-1.0, min(1.0, cos_az)))
    if math.sin(ha) > 0:
        az = 2.0 * math.pi - az
    return math.degrees(alt), math.degrees(az)


def altaz_curve(ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float,
                t0: datetime, t1: datetime, step_min: int = 15) -> list:
    """[(when, alt_deg, az_deg), ...] — sim replacement for
    cloud.conditions.altaz_curve."""
    n = max(2, int((t1 - t0).total_seconds() / 60 / step_min) + 1)
    out = []
    for i in range(n):
        when = t0 + timedelta(minutes=i * step_min)
        alt, az = altaz(ra_deg, dec_deg, lat_deg, lon_deg, when)
        out.append((when, alt, az))
    return out


# ── Sun ───────────────────────────────────────────────────────────────────────

def sun_radec(when: datetime) -> tuple:
    """Low-precision solar RA/Dec in degrees (same series as
    cloud.chorus.horizon.sun_ra_deg, extended with declination)."""
    n = days_since_j2000(when)
    L = math.radians((280.460 + 0.9856474 * n) % 360.0)
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = (L + math.radians(1.915) * math.sin(g)
           + math.radians(0.020) * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    return math.degrees(ra) % 360.0, math.degrees(dec)


def sun_alt(lat_deg: float, lon_deg: float, when: datetime) -> float:
    ra, dec = sun_radec(when)
    alt, _ = altaz(ra, dec, lat_deg, lon_deg, when)
    return alt


def night_window(lat_deg: float, lon_deg: float, start_utc: datetime,
                 sun_limit_deg: float = -12.0,
                 step_min: int = 10) -> Optional[tuple]:
    """Next contiguous dark stretch (sun below `sun_limit_deg`) within 24 h of
    `start_utc`, or None (polar day).  Mirrors cloud.conditions.night_window."""
    n = int(24 * 60 / step_min) + 1
    dark = [sun_alt(lat_deg, lon_deg,
                    start_utc + timedelta(minutes=i * step_min)) < sun_limit_deg
            for i in range(n)]
    if not any(dark):
        return None
    i0 = dark.index(True)
    i1 = i0
    while i1 + 1 < n and dark[i1 + 1]:
        i1 += 1
    t0 = start_utc + timedelta(minutes=i0 * step_min)
    t1 = start_utc + timedelta(minutes=i1 * step_min)
    if t1 <= t0:
        return None
    return t0, t1


# ── Moon (low-precision: enough for illumination and rough separation) ───────

def moon_state(when: datetime) -> dict:
    """{"illumination", "ra_deg", "dec_deg"} — Meeus truncated series, a few
    degrees of position error and ~2% illumination error: comfortably inside
    what the σ model needs (moon terms enter as a sky-brightness nudge)."""
    n = days_since_j2000(when)
    # Mean elements (deg).
    Lm = (218.316 + 13.176396 * n) % 360.0        # mean longitude
    Mm = math.radians((134.963 + 13.064993 * n) % 360.0)   # mean anomaly
    F = math.radians((93.272 + 13.229350 * n) % 360.0)     # argument of latitude
    lam = math.radians(Lm) + math.radians(6.289) * math.sin(Mm)
    beta = math.radians(5.128) * math.sin(F)
    eps = math.radians(23.439)
    ra = math.atan2(
        math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps),
        math.cos(lam))
    dec = math.asin(math.sin(beta) * math.cos(eps)
                    + math.cos(beta) * math.sin(eps) * math.sin(lam))
    ra_deg = math.degrees(ra) % 360.0
    dec_deg = math.degrees(dec)
    # Illumination from elongation to the sun (same formula as conditions.py).
    sun_ra, sun_dec = sun_radec(when)
    elong = angular_separation_deg(ra_deg, dec_deg, sun_ra, sun_dec)
    illum = (1.0 - math.cos(math.radians(elong))) / 2.0
    return {"illumination": illum, "ra_deg": ra_deg, "dec_deg": dec_deg}


def angular_separation_deg(ra1: float, dec1: float,
                           ra2: float, dec2: float) -> float:
    """Great-circle separation in degrees (identical to cloud.conditions)."""
    ra1, dec1, ra2, dec2 = map(math.radians, (ra1, dec1, ra2, dec2))
    cos_sep = (math.sin(dec1) * math.sin(dec2)
               + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))
