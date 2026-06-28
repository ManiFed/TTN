#!/usr/bin/env python3
"""
Network-optimizer objective — the marginal-value model.

The scheduler decides *who observes what, when* across the whole fleet by
repeatedly committing the placement with the highest **marginal value**:

    marginal_value = base_value                      # stored (target, node) score
                   × slot_quality[slot]              # time-of-night refinement (Q)
                   × redundancy_factor(...)          # ↓ for each network repeat (M)
                   × cadence_bonus(...)              # ↑ for well-spaced samples (M)

`base_value` and `slot_quality` are fixed once an opportunity is built
(scoring + Stage A); `redundancy_factor` and `cadence_bonus` depend on what the
network has *already* committed for that target tonight, so they change as the
greedy assignment proceeds — that coupling is what produces real coordination
(avoid piling onto one bright target) and cadence coverage (spread samples in
time) instead of every node greedily grabbing the same best targets.

Everything here is pure and deterministic: no DB, no astropy, no LLM.  The
tunable coefficients (slot-quality blend, redundancy decay, cadence strength,
slew model) are passed in as plain dicts by the planner, which reads them live
from the AI-tuned `tuning.active_params()`.  Keeping this module dependency-free
makes the coordination logic unit-testable in isolation.
"""

import math
from datetime import datetime
from typing import Iterable, Mapping, Optional, Sequence

# Sensible fallbacks so callers (and tests) can omit the tuned dicts.
DEFAULT_SLOT_WEIGHTS = {
    "altitude":      0.35,   # airmass / how high the target rides at this slot
    "cloud":         0.20,   # per-slot clear-sky fraction
    "seeing":        0.15,   # per-slot atmospheric steadiness
    "transparency":  0.10,   # per-slot extinction
    "moon":          0.10,   # per-slot moon separation × illumination
    "sky_brightness": 0.05,  # darkness at this slot (residual after night-avg)
    "thermal":       0.05,   # camera thermal headroom (uncooled penalty)
}

DEFAULT_COORD_PARAMS = {
    "redundancy_decay":           0.55,   # geometric value retained per extra network obs
    "redundancy_diversity_sep":   90.0,   # longitude sep (deg) for full diversity credit
    "cadence_bonus_strength":     1.0,    # 0 = ignore cadence, 1 = full cadence shaping
    "slew_cost_weight":           1.0,    # multiplier on slew/overhead in sequencing
    "slew_deg_per_s":             3.0,    # mount slew rate (deg/s)
    "slew_settle_s":              15.0,   # settle + plate-solve per slew
    "filter_change_cost_s":       60.0,   # added overhead when the band changes
    "meridian_flip_cost_s":       120.0,  # equatorial meridian-flip gap
    "preferred_target_boost":     0.10,   # base-value uplift for operator preferred types
    "robustness_cloud_relax":     0.20,   # cloud-cutoff relaxation for enclosure/dew nodes
    "slot_quality_floor":         0.50,   # worst feasible slot keeps this fraction of value
    "local_search_aggressiveness": 1.0,   # scales SA iterations / temperature
}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# ── Slot quality (Q) ─────────────────────────────────────────────────────────

def slot_quality(factors: Mapping[str, float],
                 weights: Optional[Mapping[str, float]] = None) -> float:
    """
    Weighted blend of the time-resolved 0..1 quality factors at one slot
    (altitude, cloud, seeing, transparency, moon, sky_brightness, thermal).
    Missing factors are treated as neutral-absent (skipped), so a node with no
    seeing forecast simply weights the factors it does have.
    """
    w = weights or DEFAULT_SLOT_WEIGHTS
    num = 0.0
    den = 0.0
    for key, weight in w.items():
        if key in factors and factors[key] is not None:
            num += float(weight) * clamp01(factors[key])
            den += float(weight)
    if den <= 0:
        return 0.0
    return clamp01(num / den)


def normalize_slot_quality(raw_by_slot: Mapping[int, float], floor: float = 0.5) -> dict:
    """
    Rescale a target's per-slot raw quality so its best feasible slot reads 1.0
    and the worst keeps `floor`.  This makes slot_quality a *within-night
    placement* refinement (pick the best hours for this target) rather than a
    second copy of the night-average sky quality already inside base_value —
    avoiding double-counting while still steering each target to its best slot.
    """
    if not raw_by_slot:
        return {}
    peak = max(raw_by_slot.values())
    if peak <= 0:
        # No usable quality signal — treat all feasible slots as equal.
        return {s: 1.0 for s in raw_by_slot}
    floor = clamp01(floor)
    return {s: floor + (1.0 - floor) * (q / peak) for s, q in raw_by_slot.items()}


# ── Redundancy (M) — diminishing returns across the fleet ────────────────────

def redundancy_factor(n_already_assigned: int,
                      longitude_sep_deg: float,
                      coord: Optional[Mapping[str, float]] = None) -> float:
    """
    Value multiplier for the (n+1)-th network observation of a target tonight.

    First observation → 1.0.  Each additional observation decays geometrically
    by `redundancy_decay`, but a well-separated one (far in longitude from the
    nodes already covering the target) recovers part of that value, because
    longitudinally spread coverage extends the monitored time-baseline rather
    than just duplicating a measurement.  Always < 1.0 for repeats, and strictly
    decreasing in the repeat count at a fixed separation.
    """
    if n_already_assigned <= 0:
        return 1.0
    c = coord or DEFAULT_COORD_PARAMS
    decay = float(c.get("redundancy_decay", DEFAULT_COORD_PARAMS["redundancy_decay"]))
    full_sep = float(c.get("redundancy_diversity_sep",
                           DEFAULT_COORD_PARAMS["redundancy_diversity_sep"]))
    base = decay ** n_already_assigned
    sep_frac = clamp01(longitude_sep_deg / full_sep) if full_sep > 0 else 0.0
    # Diversity recovers up to half of the gap between the decayed value and 1.0,
    # so repeats stay penalized but spread coverage is rewarded over clustering.
    return clamp01(base + (1.0 - base) * sep_frac * 0.5)


# ── Cadence (M) — reward filling empty time bins ─────────────────────────────

def cadence_bonus(candidate_utc: datetime,
                  assigned_utcs: Sequence[datetime],
                  cadence_hours: float,
                  coord: Optional[Mapping[str, float]] = None) -> float:
    """
    Value multiplier rewarding an observation that lands a full cadence away
    from the target's already-scheduled samples (fills a new time bin) and
    penalizing one that clusters next to an existing sample.

    First sample → 1.0.  Gap ≥ cadence → 1.0.  Coincident → 1 − strength
    (so strength=0 disables cadence shaping, strength=1 fully suppresses
    clustered repeats).  Drives time-series quality and continuous coverage.
    """
    if not assigned_utcs:
        return 1.0
    c = coord or DEFAULT_COORD_PARAMS
    strength = clamp01(c.get("cadence_bonus_strength",
                             DEFAULT_COORD_PARAMS["cadence_bonus_strength"]))
    cadence_hours = max(float(cadence_hours or 24.0), 1e-6)
    nearest_gap_h = min(
        abs((candidate_utc - t).total_seconds()) / 3600.0 for t in assigned_utcs)
    ratio = clamp01(nearest_gap_h / cadence_hours)
    return clamp01(1.0 - strength * (1.0 - ratio))


# ── Composite marginal value ─────────────────────────────────────────────────

def marginal_value(base_value: float,
                   slot_q: float,
                   n_already_assigned: int,
                   longitude_sep_deg: float,
                   candidate_utc: datetime,
                   assigned_utcs: Sequence[datetime],
                   cadence_hours: float,
                   coord: Optional[Mapping[str, float]] = None) -> float:
    """The full marginal value of placing `opportunity` at a slot, given the
    target's currently-committed network observations."""
    return (
        max(0.0, float(base_value))
        * clamp01(slot_q)
        * redundancy_factor(n_already_assigned, longitude_sep_deg, coord)
        * cadence_bonus(candidate_utc, assigned_utcs, cadence_hours, coord)
    )


# ── Slew / sequencing overhead ───────────────────────────────────────────────

def slew_seconds(separation_deg: float,
                 coord: Optional[Mapping[str, float]] = None) -> float:
    """Slew + settle time for a move of `separation_deg` across the sky."""
    c = coord or DEFAULT_COORD_PARAMS
    rate = max(float(c.get("slew_deg_per_s", DEFAULT_COORD_PARAMS["slew_deg_per_s"])), 0.1)
    settle = float(c.get("slew_settle_s", DEFAULT_COORD_PARAMS["slew_settle_s"]))
    return settle + max(0.0, float(separation_deg)) / rate


def transition_overhead_seconds(sep_deg: float,
                                filter_changed: bool,
                                meridian_flip: bool,
                                coord: Optional[Mapping[str, float]] = None) -> float:
    """Total overhead between two consecutive observations: slew + (optional)
    filter change + (optional) equatorial meridian-flip gap."""
    c = coord or DEFAULT_COORD_PARAMS
    total = slew_seconds(sep_deg, c)
    if filter_changed:
        total += float(c.get("filter_change_cost_s",
                             DEFAULT_COORD_PARAMS["filter_change_cost_s"]))
    if meridian_flip:
        total += float(c.get("meridian_flip_cost_s",
                             DEFAULT_COORD_PARAMS["meridian_flip_cost_s"]))
    return total


# ── Pure geometry helpers for sequencing (testable without astropy) ───────────

def _sep(a: tuple, b: tuple) -> float:
    """Great-circle separation (deg) between (ra_deg, dec_deg) points."""
    ra1, dec1 = math.radians(a[0]), math.radians(a[1])
    ra2, dec2 = math.radians(b[0]), math.radians(b[1])
    cos_sep = (math.sin(dec1) * math.sin(dec2)
               + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


def path_length(points: Sequence[tuple]) -> float:
    """Total angular path (deg) visiting `points` in the given order."""
    return sum(_sep(points[i], points[i + 1]) for i in range(len(points) - 1))


def two_opt_order(points: Sequence[tuple], max_passes: int = 50) -> list:
    """
    Order indices of `points` (each an (ra_deg, dec_deg) tuple) to minimize total
    angular slew: nearest-neighbour seed from the first point, then 2-opt
    improvement.  Returns a list of indices into `points`.  Deterministic.
    """
    n = len(points)
    if n <= 2:
        return list(range(n))

    # Nearest-neighbour seed.
    unvisited = set(range(1, n))
    order = [0]
    while unvisited:
        last = order[-1]
        nxt = min(unvisited, key=lambda j: _sep(points[last], points[j]))
        order.append(nxt)
        unvisited.discard(nxt)

    def tour(o):
        return path_length([points[i] for i in o])

    best = tour(order)
    for _ in range(max_passes):
        improved = False
        for i in range(1, n - 1):
            for k in range(i + 1, n):
                cand = order[:i] + order[i:k + 1][::-1] + order[k + 1:]
                d = tour(cand)
                if d + 1e-9 < best:
                    order, best, improved = cand, d, True
        if not improved:
            break
    return order
