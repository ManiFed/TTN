#!/usr/bin/env python3
"""
CHORUS horizon tier (T1) — pricing tonight against the future (CHORUS.md §2.5).

For each target, a deterministic sweep over the next H nights estimates how
capturable it remains: analytic visibility from each capable node (transit
altitude + angular distance from the sun's RA), times that site's
climatological clear probability for the month, times the node's execution
probability.  The scarcity multiplier

    S = 1 / (1 + Σ_d γ^d · q(d))

is ≈1 for a use-it-or-lose-it target (season ending, rare transit) and small
for one the network can capture any night for months (circumpolar LPVs — the
fleet's ballast and the nursery for new nodes).

Pure stdlib + math; climatology and p_exec are passed in by the planner.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

MIN_SUN_RA_SEP_DEG = 50.0   # closer than this to the sun's RA → not observable


def sun_ra_deg(when: datetime) -> float:
    """Low-precision solar RA (deg) — plenty for a seasonal visibility gate."""
    j2000 = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)
    n = (when - j2000).total_seconds() / 86400.0
    L = math.radians((280.460 + 0.9856474 * n) % 360.0)
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = L + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    return math.degrees(ra) % 360.0


def _ra_sep_deg(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def visible_from(node: dict, ra_deg: float, dec_deg: float,
                 when: datetime) -> bool:
    """Coarse night-of visibility: the target transits above the node's
    altitude floor and sits far enough from the sun in RA to be up in the dark."""
    lat = float(node.get("latitude", 0.0))
    min_alt = float(node.get("min_altitude_deg", 25.0) or 25.0)
    transit_alt = 90.0 - abs(lat - dec_deg)
    if transit_alt < min_alt:
        return False
    return _ra_sep_deg(ra_deg, sun_ra_deg(when)) > MIN_SUN_RA_SEP_DEG


def scarcity(target: dict, nodes: list, p_exec_by_node: dict,
             clear_prob: Callable[[dict, datetime], float],
             params: dict, today: Optional[datetime] = None) -> float:
    """S ∈ (0, 1] for one target given the fleet's future capture chances.

    clear_prob(node, when) → climatological clear probability for that site
    that month (ledger.climatology_fn provides it; tests can pass a constant).
    """
    if not nodes:
        return 1.0
    gamma = min(max(float(params.get("scarcity_gamma", 0.93)), 0.5), 0.999)
    horizon = int(float(params.get("scarcity_horizon_nights", 45)))
    now = today or datetime.now(timezone.utc)
    ra, dec = float(target["ra_deg"]), float(target["dec_deg"])

    q = 0.0
    for d in range(1, horizon + 1):
        when = now + timedelta(days=d)
        best = 0.0
        for node in nodes:
            if not visible_from(node, ra, dec, when):
                continue
            p = (clear_prob(node, when)
                 * float(p_exec_by_node.get(node.get("node_id", ""), 0.6)))
            if p > best:
                best = p
        q += (gamma ** d) * best
    return 1.0 / (1.0 + q)


def event_scarcity(period_days: Optional[float], target: dict, nodes: list,
                   p_exec_by_node: dict,
                   clear_prob: Callable[[dict, datetime], float],
                   params: dict, today: Optional[datetime] = None) -> float:
    """Scarcity for a periodic *event* (transit): future chances only exist on
    nights an event recurs, so rare events price near 1.0."""
    if not period_days or period_days <= 0 or not nodes:
        return 1.0
    gamma = min(max(float(params.get("scarcity_gamma", 0.93)), 0.5), 0.999)
    horizon = int(float(params.get("scarcity_horizon_nights", 45)))
    now = today or datetime.now(timezone.utc)
    ra, dec = float(target["ra_deg"]), float(target["dec_deg"])

    q = 0.0
    k = 1
    while True:
        d = k * period_days
        if d > horizon:
            break
        when = now + timedelta(days=d)
        best = 0.0
        for node in nodes:
            if not visible_from(node, ra, dec, when):
                continue
            p = (clear_prob(node, when)
                 * float(p_exec_by_node.get(node.get("node_id", ""), 0.6)))
            if p > best:
                best = p
        q += (gamma ** d) * best
        k += 1
    return 1.0 / (1.0 + q)
