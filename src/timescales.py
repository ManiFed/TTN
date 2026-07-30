"""
Time-scale conversions for AAVSO reporting.

The pipeline timestamps every measurement as BJD_TDB — barycentric Julian date
on the barycentric dynamical time scale.  That is the right internal choice: it
is the only timestamp that is free of both the Earth's orbital position (±8.3
minutes) and the relativistic wander of terrestrial clocks, so measurements
from different nodes on different nights are directly comparable.

The AAVSO Extended File Format does not accept it.  `#DATE=` takes JD, HJD, or
EXCEL — BJD is accepted only in the separate Exoplanet Report format.  So the
value written into a WebObs submission has to be converted, and converted
properly: BJD_TDB and HJD_UTC differ by the TDB−UTC offset (~69 s in 2026) plus
the difference between the barycentric and heliocentric light-travel terms (up
to a few seconds).  Relabelling the header without touching the number would
put a systematic error of about a minute into the AAVSO archive.

Conversion is by inversion: recover the observed UTC instant from BJD_TDB, then
re-derive the heliocentric correction at that instant.  Two iterations are
plenty — the light-travel term changes by well under a millisecond over the
sub-second residual of the first pass.
"""

from __future__ import annotations

import logging

import astropy.units as u
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time

logger = logging.getLogger("timescales")


def _geocenter() -> EarthLocation:
    """astropy needs an explicit location; the Earth's centre is the neutral
    choice when the observing site is unknown (worth ≤21 ms either way)."""
    return EarthLocation.from_geocentric(0.0 * u.m, 0.0 * u.m, 0.0 * u.m)


def heliocentric_jd_utc(t: Time, ra_deg: float, dec_deg: float,
                        location: EarthLocation | None = None) -> float:
    """HJD_UTC for an observation of (ra_deg, dec_deg) at time `t`.

    `t` is the observed instant (mid-exposure); its own scale is irrelevant,
    only the epoch matters.  The observer's position contributes ≤21 ms, so
    the geocenter is an acceptable default when the site is unknown.
    """
    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    loc = location if location is not None else _geocenter()
    ltt = t.light_travel_time(coord, kind="heliocentric", location=loc)
    return float((t.utc + ltt).jd)


def hjd_utc_from_bjd_tdb(bjd_tdb: float, ra_deg: float, dec_deg: float,
                         location: EarthLocation | None = None) -> float:
    """Convert a stored BJD_TDB back to HJD_UTC.

    Used for measurements that predate the node reporting HJD directly.  Raises
    nothing: a caller that cannot supply real coordinates must not call this,
    because a wrong direction moves the timestamp by minutes.
    """
    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    location = location if location is not None else _geocenter()

    # Invert: guess the observed instant, then correct the guess with the
    # barycentric term evaluated there.
    t = Time(bjd_tdb, format="jd", scale="tdb")
    for _ in range(2):
        ltt_bary = t.light_travel_time(coord, kind="barycentric", location=location)
        t = Time(bjd_tdb, format="jd", scale="tdb") - ltt_bary

    ltt_helio = t.light_travel_time(coord, kind="heliocentric", location=location)
    return float((t.utc + ltt_helio).jd)
