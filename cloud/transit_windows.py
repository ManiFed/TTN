#!/usr/bin/env python3
"""
Transit window computation for scheduled exoplanet photometry.

get_tonight_transits(t0, t1, min_alt_deg, lat, lon) queries the
transit_ephemerides table and returns every transit that overlaps tonight's
dark window, together with the recommended observation start/end times
(mid-transit ± duration/2 + PRE/POST_BASELINE_MIN of out-of-transit baseline).

JD ≈ BJD for scheduling purposes (< 8 min difference, within our 15 min slot
resolution).
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cloud import db
from cloud.conditions import altitude_curve

logger = logging.getLogger("cloud.transit_windows")

# Baseline on each side of the transit for normalization
PRE_BASELINE_MIN = 30
POST_BASELINE_MIN = 30

JD_UNIX_EPOCH = 2440587.5  # JD at 1970-01-01 00:00 UTC


def _jd_now() -> float:
    return datetime.now(timezone.utc).timestamp() / 86400 + JD_UNIX_EPOCH


def _jd_to_utc(jd: float) -> datetime:
    unix = (jd - JD_UNIX_EPOCH) * 86400
    return datetime.fromtimestamp(unix, tz=timezone.utc)


def _utc_to_jd(dt: datetime) -> float:
    return dt.timestamp() / 86400 + JD_UNIX_EPOCH


@dataclass
class TransitWindow:
    target_id: str
    name: str
    ra_deg: float
    dec_deg: float
    mag: float
    t_mid_utc: datetime        # mid-transit
    obs_start_utc: datetime    # recommended start (pre-ingress baseline)
    obs_end_utc: datetime      # recommended end (post-egress baseline)
    duration_hours: float
    depth_ppt: float
    period_days: float


def get_tonight_transits(
    t0: datetime,
    t1: datetime,
    lat_deg: float,
    lon_deg: float,
    min_alt_deg: float = 25.0,
) -> list[TransitWindow]:
    """
    Find transiting exoplanets whose transit window overlaps [t0, t1] (UTC)
    and whose host star is above min_alt_deg during the full observation.

    Returns TransitWindow objects sorted by obs_start_utc.
    """
    rows = db.query(
        """SELECT t.target_id, t.name, t.ra_deg, t.dec_deg, t.mag,
                  e.period_days, e.epoch_bjd, e.duration_hours, e.depth_ppt
             FROM transit_ephemerides e
             JOIN targets t ON t.target_id = e.target_id
            WHERE t.active = 1 AND e.period_days > 0"""
    )

    jd0 = _utc_to_jd(t0)
    jd1 = _utc_to_jd(t1)
    pre_d = PRE_BASELINE_MIN / 1440.0
    post_d = POST_BASELINE_MIN / 1440.0

    results: list[TransitWindow] = []
    for row in rows:
        period = row["period_days"]
        epoch = row["epoch_bjd"]
        dur_h = row["duration_hours"]
        dur_d = dur_h / 24.0

        # Find transit nearest to tonight's midpoint
        night_mid = (jd0 + jd1) / 2
        n = round((night_mid - epoch) / period)
        for dn in (n - 1, n, n + 1):
            t_mid_jd = epoch + dn * period
            obs_start_jd = t_mid_jd - dur_d / 2 - pre_d
            obs_end_jd = t_mid_jd + dur_d / 2 + post_d

            # Observation window must overlap the dark window
            if obs_end_jd < jd0 or obs_start_jd > jd1:
                continue

            # Clamp to dark window
            obs_start_jd = max(obs_start_jd, jd0)
            obs_end_jd = min(obs_end_jd, jd1)

            obs_start = _jd_to_utc(obs_start_jd)
            obs_end = _jd_to_utc(obs_end_jd)
            t_mid = _jd_to_utc(t_mid_jd)

            # Check star altitude throughout the observation
            curve = altitude_curve(
                row["ra_deg"], row["dec_deg"], lat_deg, lon_deg,
                obs_start, obs_end, step_min=15,
            )
            if not curve or min(a for _, a in curve) < min_alt_deg:
                continue

            results.append(TransitWindow(
                target_id=row["target_id"],
                name=row["name"],
                ra_deg=row["ra_deg"],
                dec_deg=row["dec_deg"],
                mag=float(row["mag"] or 10.0),
                t_mid_utc=t_mid,
                obs_start_utc=obs_start,
                obs_end_utc=obs_end,
                duration_hours=dur_h,
                depth_ppt=float(row["depth_ppt"] or 0),
                period_days=period,
            ))
            break  # found one transit per target for tonight

    results.sort(key=lambda w: w.obs_start_utc)
    if results:
        logger.info("Transit windows tonight: %d", len(results))
    return results
