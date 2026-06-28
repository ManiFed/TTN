#!/usr/bin/env python3
"""
Network-level plan generation — the optimizer that replaces the per-node greedy
slot-packer.

For each global night-cycle it runs four stages:

  A. build_opportunities()  per node: every feasible (target, slot-set, value)
     opportunity, after the (F) feasibility filters (dark window, local horizon
     mask, fit) and with a time-resolved (Q) slot-quality array.
  B. assign_network()       fleet-wide assignment maximizing total *marginal*
     value (objective.marginal_value): a global greedy seed that bakes in
     diminishing returns per target, then a time-boxed annealing refinement.
     This is what produces real coordination (avoid redundant double-coverage)
     and cadence spread, instead of every node grabbing the same best targets.
  C. sequence_node()        per node: order the committed observations in time to
     cut slew / filter-change / meridian-flip overhead (NN + 2-opt), emit
     PlanItems in node-local time.
  D. persist               reuse scheduler._save_plan + the plans table, so the
     node agent and all existing APIs are unaffected.

The objective coefficients (slot-quality blend, redundancy decay, cadence
strength, slew model) are read live from the AI-tuned tuning.active_params(), so
the nightly Claude monitor reshapes the optimizer without a restart.

Enabled by scheduler.network_optimizer; scheduler.generate_all_plans delegates
here when the flag is on, and falls back to the legacy greedy packer when off.
"""

import json
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from cloud import db, incidents, objective, registry, tuning
from cloud.conditions import (
    airmass_from_alt, altaz_curve, angular_separation_deg,
    astro_cloud_cover_at, cloud_cover_at, fetch_astronomy_weather, fetch_weather,
    horizon_min_alt, moon_state, night_window, seeing_score_at,
    transparency_score_at,
)
from cloud.scheduler import _save_plan, choose_exposure, _longitude_sep
from cloud.transit_windows import get_tonight_transits
from src.shared_models import ObservationPlan, PlanItem

logger = logging.getLogger("cloud.network_planner")

STEP_MIN = 15                 # slot granularity, minutes (matches legacy scheduler)
SLEW_RESERVE_MIN = 5.0        # occupancy reserve per target; real slew optimised in Stage C
TRANSIT_BASE_VALUE = 0.85     # floor value for a transit opportunity


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class NodeContext:
    node: dict
    node_id: str
    lat: float
    lon: float
    t0: datetime
    t1: datetime
    n_slots: int
    utc_offset: timedelta
    min_alt: float
    horizon_mask: list
    filters: list
    cooled: bool
    mount_type: str
    cloud_relax: float          # robustness: cloud-cutoff relaxation (enclosure/dew)
    max_targets: int

    def slot_utc(self, slot: int) -> datetime:
        return self.t0 + timedelta(minutes=slot * STEP_MIN)


@dataclass
class Opportunity:
    node_id: str
    target_id: str
    name: str
    ra_deg: float
    dec_deg: float
    mag: Optional[float]
    target_type: str
    base_value: float
    exp_dur: float
    exp_count: int
    need: int                              # occupancy in slots
    feasible: dict                         # slot -> normalized slot quality (0..1)
    az_by_slot: dict                       # slot -> azimuth deg (for meridian flip)
    filter: str
    longitude: float
    cadence_hours: float
    components: dict = field(default_factory=dict)
    is_transit: bool = False
    pinned_slot: Optional[int] = None      # transits: only this start slot
    duration_min: float = 0.0
    observation_mode: str = "single_epoch"

    @property
    def key(self) -> tuple:
        return (self.node_id, self.target_id)


# ── Stage A: opportunities ───────────────────────────────────────────────────

def build_node_context(node: dict, config: dict) -> Optional[NodeContext]:
    """Dark-window + capability context for one node, or None when the node is
    not observing tonight (vacation / no darkness within 24 h)."""
    # Vacation: skip nodes explicitly parked.
    vac = (node.get("vacation_until") or "").strip()
    if vac:
        try:
            if datetime.fromisoformat(vac).date() >= datetime.now(timezone.utc).date():
                logger.info("Node %s on vacation until %s — skipping", node["node_id"], vac)
                return None
        except ValueError:
            pass
    if node.get("status") in ("disabled", "vacation"):
        return None

    # Portable nodes use tonight's session location when set.
    lat = float(node.get("latitude", 0.0))
    lon = float(node.get("longitude", 0.0))
    if node.get("portable") and (node.get("session_lat") or node.get("session_lon")):
        lat = float(node.get("session_lat") or lat)
        lon = float(node.get("session_lon") or lon)

    sun_limit = float(config.get("scheduler", {}).get("sun_altitude_limit", -12.0))
    night = night_window(lat, lon, sun_limit_deg=sun_limit)
    if night is None:
        logger.info("No darkness for %s within 24 h — no plan", node["node_id"])
        return None
    t0, t1 = night

    n_slots = max(1, int((t1 - t0).total_seconds() / 60 / STEP_MIN))
    coord = tuning.active_params(config).get("coordination", objective.DEFAULT_COORD_PARAMS)
    relax = float(coord.get("robustness_cloud_relax",
                            objective.DEFAULT_COORD_PARAMS["robustness_cloud_relax"]))
    robust = bool(node.get("has_enclosure")) or bool(node.get("has_dew_heater"))

    return NodeContext(
        node=node,
        node_id=node["node_id"],
        lat=lat,
        lon=lon,
        t0=t0,
        t1=t1,
        n_slots=n_slots,
        utc_offset=timedelta(hours=float(node.get("utc_offset_hours", 0.0))),
        min_alt=float(node.get("min_altitude_deg", 25.0)),
        horizon_mask=db.loads(node.get("horizon_mask"), []) or [],
        filters=_node_filters(node),
        cooled=bool(node.get("cooled_camera")),
        mount_type=str(node.get("mount_type", "alt_az")),
        cloud_relax=relax if robust else 0.0,
        max_targets=int(config.get("scheduler", {}).get("max_targets_per_night", 12)),
    )


def _node_filters(node: dict) -> list:
    fs = db.loads(node.get("filter_set"), None)
    if isinstance(fs, list) and fs:
        return [str(f).strip() for f in fs]
    return [f.strip() for f in str(node.get("filters") or "CV").split(",") if f.strip()]


def _pick_filter(target: dict, node_filters: list) -> str:
    band = (target.get("mag_band") or "").strip()
    if band and band in node_filters:
        return band
    return node_filters[0] if node_filters else "CV"


def _thermal_factor(ctx: NodeContext, mag: Optional[float]) -> float:
    """Cooled cameras are unpenalized; uncooled ones lose quality on faint targets
    where thermal noise bites hardest."""
    if ctx.cooled:
        return 1.0
    m = 13.0 if mag is None else float(mag)
    return objective.clamp01(1.0 - 0.15 * objective.clamp01((m - 12.0) / 4.0))


def _moon_factor_at(ra_deg: float, dec_deg: float, moon: dict) -> float:
    sep = angular_separation_deg(ra_deg, dec_deg, moon["ra_deg"], moon["dec_deg"])
    illum = moon["illumination"]
    if sep < 10.0:
        return 0.05
    proximity = max(0.0, 1.0 - sep / 90.0)
    return objective.clamp01(1.0 - illum * proximity)


def _cloud_clear_at(astro, generic, when, relax: float) -> Optional[float]:
    """Per-slot clear-sky fraction (0..1), with a robustness relaxation that
    nudges enclosure/dew-heater nodes toward schedulable in marginal cloud."""
    cc = astro_cloud_cover_at(astro, when) if astro is not None else cloud_cover_at(generic, when)
    if cc is None:
        return None
    return objective.clamp01((1.0 - cc) + relax)


def build_opportunities(ctx: NodeContext, config: dict, params: dict) -> list:
    """All feasible opportunities for one node tonight (scored targets + transits)."""
    sched_cfg = config.get("scheduler", {})
    min_score = float(sched_cfg.get("min_score", 0.25))
    slot_weights = params.get("slot_quality", objective.DEFAULT_SLOT_WEIGHTS)
    coord = params.get("coordination", objective.DEFAULT_COORD_PARAMS)
    floor = float(coord.get("slot_quality_floor",
                            objective.DEFAULT_COORD_PARAMS["slot_quality_floor"]))
    pref_boost = float(coord.get("preferred_target_boost",
                                 objective.DEFAULT_COORD_PARAMS["preferred_target_boost"]))
    preferred = set(db.loads(ctx.node.get("preferred_targets"), []) or [])

    rows = db.query(
        """SELECT s.total, s.components, t.* FROM scores s
           JOIN targets t ON t.target_id = s.target_id
           WHERE s.node_id = %s AND t.active = 1 AND s.total >= %s
           ORDER BY s.total DESC LIMIT 60""",
        (ctx.node_id, min_score),
    )

    # Weather forecasts (cached inside conditions); fetched once per node.
    astro = fetch_astronomy_weather(ctx.lat, ctx.lon)
    generic = fetch_weather(ctx.lat, ctx.lon) if astro is None else None
    # Moon sampled at start/mid/end and nearest-matched per slot (cheap, moon
    # moves slowly relative to a night).
    mid = ctx.t0 + (ctx.t1 - ctx.t0) / 2
    moon_samples = [(ctx.t0, moon_state(ctx.t0)),
                    (mid, moon_state(mid)),
                    (ctx.t1, moon_state(ctx.t1))]

    def moon_at(when):
        return min(moon_samples, key=lambda ms: abs((ms[0] - when).total_seconds()))[1]

    opps: list = []
    for row in rows:
        curve = altaz_curve(row["ra_deg"], row["dec_deg"], ctx.lat, ctx.lon,
                            ctx.t0, ctx.t1, step_min=STEP_MIN)
        alts = [c[1] for c in curve]
        azs = [c[2] for c in curve]

        exp_dur, exp_count = choose_exposure(row["mag"], ctx.node)
        obs_min = exp_dur * exp_count / 60.0
        need = max(1, math.ceil((obs_min + SLEW_RESERVE_MIN) / STEP_MIN))
        if need > ctx.n_slots:
            continue

        # Per-slot single-slot quality factor + horizon clearance.
        clear = [False] * ctx.n_slots
        per_slot_q = [0.0] * ctx.n_slots
        for s in range(min(ctx.n_slots, len(alts))):
            alt, az = alts[s], azs[s]
            req = max(ctx.min_alt, horizon_min_alt(ctx.horizon_mask, az))
            if alt < req:
                continue
            clear[s] = True
            when = ctx.slot_utc(s)
            cl = _cloud_clear_at(astro, generic, when, ctx.cloud_relax)
            factors = {
                "altitude": objective.clamp01((3.0 - airmass_from_alt(alt)) / 2.0),
                "moon": _moon_factor_at(row["ra_deg"], row["dec_deg"], moon_at(when)),
                "thermal": _thermal_factor(ctx, row["mag"]),
            }
            if cl is not None:
                factors["cloud"] = cl
            sv = seeing_score_at(astro, when)
            if sv is not None:
                factors["seeing"] = sv
            tv = transparency_score_at(astro, when)
            if tv is not None:
                factors["transparency"] = tv
            per_slot_q[s] = objective.slot_quality(factors, slot_weights)

        # Window feasibility: every slot in [s, s+need) must clear the horizon;
        # window quality is the mean per-slot quality across the dwell.
        raw_window = {}
        for s in range(0, ctx.n_slots - need + 1):
            if not all(clear[s:s + need]):
                continue
            raw_window[s] = sum(per_slot_q[s:s + need]) / need
        if not raw_window:
            continue

        base = float(row["total"])
        if row.get("target_type") in preferred:
            base = objective.clamp01(base + pref_boost)

        opps.append(Opportunity(
            node_id=ctx.node_id,
            target_id=row["target_id"],
            name=row["name"],
            ra_deg=row["ra_deg"],
            dec_deg=row["dec_deg"],
            mag=row.get("mag"),
            target_type=row.get("target_type", "unknown"),
            base_value=base,
            exp_dur=exp_dur,
            exp_count=exp_count,
            need=need,
            feasible=objective.normalize_slot_quality(raw_window, floor),
            az_by_slot={s: azs[s] for s in range(min(ctx.n_slots, len(azs)))},
            filter=_pick_filter(row, ctx.filters),
            longitude=ctx.lon,
            cadence_hours=float(row.get("cadence_hours", 24.0) or 24.0),
            components=db.loads(row["components"], {}),
            duration_min=round(obs_min, 1),
        ))

    opps.extend(_transit_opportunities(ctx))
    return opps


def _transit_opportunities(ctx: NodeContext) -> list:
    """Exoplanet transits as hard-window, high-value opportunities."""
    try:
        windows = get_tonight_transits(
            ctx.t0, ctx.t1, lat_deg=ctx.lat, lon_deg=ctx.lon, min_alt_deg=ctx.min_alt)
    except Exception as exc:
        logger.warning("Transit window lookup failed for %s: %s", ctx.node_id, exc)
        return []

    score_by_target = {
        r["target_id"]: float(r["total"]) for r in db.query(
            "SELECT target_id, total FROM scores WHERE node_id = %s", (ctx.node_id,))
    }

    out = []
    for tw in windows:
        obs_min = (tw.obs_end_utc - tw.obs_start_utc).total_seconds() / 60.0
        need = max(1, math.ceil(obs_min / STEP_MIN))
        s0 = max(0, int((tw.obs_start_utc - ctx.t0).total_seconds() / 60 / STEP_MIN))
        if s0 + need > ctx.n_slots:
            continue
        exp_dur = min(float(ctx.node.get("max_exposure_s", 30.0)), 30.0)
        exp_count = max(10, int(obs_min * 60 / exp_dur))
        out.append(Opportunity(
            node_id=ctx.node_id,
            target_id=tw.target_id,
            name=tw.name,
            ra_deg=tw.ra_deg,
            dec_deg=tw.dec_deg,
            mag=tw.mag,
            target_type="EXOPLANET",
            base_value=max(TRANSIT_BASE_VALUE, score_by_target.get(tw.target_id, 0.0)),
            exp_dur=exp_dur,
            exp_count=exp_count,
            need=need,
            feasible={s0: 1.0},
            az_by_slot={},
            filter=(ctx.filters[0] if ctx.filters else "CV"),
            longitude=ctx.lon,
            cadence_hours=float(tw.period_days * 24.0) if tw.period_days else 24.0,
            components={"transit": {
                "depth_ppt": tw.depth_ppt, "duration_hours": tw.duration_hours,
                "mid_utc": tw.t_mid_utc.strftime("%H:%M")}},
            is_transit=True,
            pinned_slot=s0,
            duration_min=round(obs_min, 1),
            observation_mode="time_series",
        ))
    return out


# ── Stage B: global assignment ───────────────────────────────────────────────

@dataclass
class _Placement:
    node_id: str
    opp: Opportunity
    slot: int


class _WorkingState:
    """Mutable assignment state with O(1) feasibility checks, shared by the
    greedy seed and the annealing refinement."""

    def __init__(self, contexts: dict):
        self.contexts = contexts
        self.free = {nid: [True] * ctx.n_slots for nid, ctx in contexts.items()}
        self.count = {nid: 0 for nid in contexts}
        self.placed: set = set()
        self.placements: list = []

    def slots_free(self, node_id: str, slot: int, need: int) -> bool:
        f = self.free[node_id]
        if slot + need > len(f):
            return False
        return all(f[slot:slot + need])

    def add(self, node_id: str, opp: Opportunity, slot: int) -> None:
        for s in range(slot, slot + opp.need):
            self.free[node_id][s] = False
        self.count[node_id] += 1
        self.placed.add(opp.key)
        self.placements.append(_Placement(node_id, opp, slot))

    def remove(self, p: _Placement) -> None:
        for s in range(p.slot, p.slot + p.opp.need):
            self.free[p.node_id][s] = True
        self.count[p.node_id] -= 1
        self.placed.discard(p.opp.key)
        self.placements.remove(p)


def _target_groups(placements: list, contexts: dict) -> dict:
    """Group placements by target, each sorted by UTC start time."""
    groups: dict = {}
    for p in placements:
        groups.setdefault(p.opp.target_id, []).append(p)
    for tid, ps in groups.items():
        ps.sort(key=lambda p: contexts[p.node_id].slot_utc(p.slot))
    return groups


def objective_total(placements: list, contexts: dict, coord: dict) -> float:
    """Order-independent total marginal value of an assignment: for each target,
    its i-th (time-ordered) network observation is valued with diminishing
    returns vs the earlier ones (redundancy) and a cadence bonus vs their times."""
    total = 0.0
    for ps in _target_groups(placements, contexts).values():
        prev_times: list = []
        prev_lons: list = []
        for i, p in enumerate(ps):
            ctx = contexts[p.node_id]
            t = ctx.slot_utc(p.slot)
            q = p.opp.feasible.get(p.slot, 1.0)
            lon_sep = min((_longitude_sep(p.opp.longitude, o) for o in prev_lons),
                          default=0.0)
            total += objective.marginal_value(
                p.opp.base_value, q, i, lon_sep, t, prev_times,
                p.opp.cadence_hours, coord)
            prev_times.append(t)
            prev_lons.append(p.opp.longitude)
    return total


def _best_placement_for_opp(opp, ctx, state, assigned_times, assigned_lons, coord):
    """Best (slot, marginal_value) for one opportunity given current state."""
    slots = [opp.pinned_slot] if opp.is_transit else opp.feasible.keys()
    times = assigned_times.get(opp.target_id, [])
    lons = assigned_lons.get(opp.target_id, [])
    lon_sep = min((_longitude_sep(opp.longitude, o) for o in lons), default=0.0)
    best_slot, best_mv = None, 0.0
    for s in slots:
        if s is None or not state.slots_free(opp.node_id, s, opp.need):
            continue
        q = opp.feasible.get(s, 1.0)
        mv = objective.marginal_value(
            opp.base_value, q, len(times), lon_sep, ctx.slot_utc(s), times,
            opp.cadence_hours, coord)
        if mv > best_mv:
            best_slot, best_mv = s, mv
    return best_slot, best_mv


def assign_network(contexts: dict, opps_by_node: dict, config: dict,
                   params: dict, seed: int = 1234) -> tuple:
    """
    Fleet-wide assignment maximizing total marginal value.

    Greedy seed: repeatedly commit the globally highest-marginal-value placement,
    which lowers that target's future value (redundancy) and shifts its cadence
    bins — so the next pick naturally favours uncovered targets and well-spaced
    repeats.  Then a bounded annealing pass relocates / swaps / adds to escape
    local optima.  Returns (assignments_by_node, stats).
    """
    coord = params.get("coordination", objective.DEFAULT_COORD_PARAMS)
    state = _WorkingState(contexts)
    assigned_times: dict = {}
    assigned_lons: dict = {}

    # ── Greedy seed ──────────────────────────────────────────────────────────
    while True:
        best = None  # (mv, node_id, opp, slot)
        for node_id, opps in opps_by_node.items():
            ctx = contexts[node_id]
            if state.count[node_id] >= ctx.max_targets:
                continue
            for opp in opps:
                if opp.key in state.placed:
                    continue
                slot, mv = _best_placement_for_opp(
                    opp, ctx, state, assigned_times, assigned_lons, coord)
                if slot is not None and (best is None or mv > best[0]):
                    best = (mv, node_id, opp, slot)
        if best is None or best[0] <= 0.0:
            break
        _, node_id, opp, slot = best
        state.add(node_id, opp, slot)
        assigned_times.setdefault(opp.target_id, []).append(contexts[node_id].slot_utc(slot))
        assigned_lons.setdefault(opp.target_id, []).append(opp.longitude)

    greedy_obj = objective_total(state.placements, contexts, coord)

    # ── Annealing refinement (time-boxed, never returns worse than greedy) ────
    best_placements = list(state.placements)
    best_obj = greedy_obj
    budget_ms = float(config.get("scheduler", {}).get("local_search_ms", 1500))
    aggr = float(coord.get("local_search_aggressiveness",
                           objective.DEFAULT_COORD_PARAMS["local_search_aggressiveness"]))
    if budget_ms > 0 and aggr > 0 and state.placements:
        best_obj, best_placements = _anneal(
            state, opps_by_node, contexts, coord, budget_ms, aggr, seed, greedy_obj)

    # Rebuild assignments_by_node from the best solution found.
    assignments: dict = {nid: [] for nid in contexts}
    for p in best_placements:
        assignments[p.node_id].append((p.opp, p.slot))
    for nid in assignments:
        assignments[nid].sort(key=lambda os: os[1])

    stats = {
        "greedy_objective": round(greedy_obj, 4),
        "final_objective": round(best_obj, 4),
        "n_assignments": len(best_placements),
    }
    return assignments, stats


def _anneal(state, opps_by_node, contexts, coord, budget_ms, aggr, seed, greedy_obj):
    """Metropolis local search over relocate / drop / add moves.  Operates in
    place on `state`, tracking and returning the best (objective, placements)."""
    rng = random.Random(seed)
    best_obj = greedy_obj
    best_placements = list(state.placements)
    cur_obj = greedy_obj
    temp = 0.05 * aggr                      # starting temperature
    cooling = 0.995
    deadline = time.monotonic() + budget_ms / 1000.0
    max_iters = int(4000 * aggr)
    all_opps = [o for opps in opps_by_node.values() for o in opps]

    it = 0
    while it < max_iters and time.monotonic() < deadline:
        it += 1
        move = rng.random()
        undo = None

        if move < 0.45 and state.placements:           # relocate
            p = rng.choice(state.placements)
            if p.opp.is_transit:
                continue
            alt_slots = [s for s in p.opp.feasible
                         if s != p.slot]
            if not alt_slots:
                continue
            state.remove(p)
            rng.shuffle(alt_slots)
            placed = False
            for s in alt_slots:
                if state.slots_free(p.node_id, s, p.opp.need):
                    state.add(p.node_id, p.opp, s)
                    placed = True
                    break
            if not placed:
                state.add(p.node_id, p.opp, p.slot)     # restore
                continue
        elif move < 0.65 and state.placements:          # drop
            p = rng.choice(state.placements)
            state.remove(p)
        else:                                            # add
            cand = [o for o in all_opps
                    if o.key not in state.placed
                    and state.count[o.node_id] < contexts[o.node_id].max_targets]
            if not cand:
                continue
            o = rng.choice(cand)
            slots = [o.pinned_slot] if o.is_transit else list(o.feasible.keys())
            rng.shuffle(slots)
            placed = False
            for s in slots:
                if s is not None and state.slots_free(o.node_id, s, o.need):
                    state.add(o.node_id, o, s)
                    placed = True
                    break
            if not placed:
                continue

        new_obj = objective_total(state.placements, contexts, coord)
        delta = new_obj - cur_obj
        if delta >= 0 or rng.random() < math.exp(delta / max(temp, 1e-6)):
            cur_obj = new_obj
            if new_obj > best_obj:
                best_obj = new_obj
                best_placements = list(state.placements)
        else:
            # Reject: restore the best-known solution to keep the walk anchored.
            _restore(state, best_placements)
            cur_obj = best_obj
        temp *= cooling

    return best_obj, best_placements


def _restore(state, placements):
    """Reset `state` to exactly the given placement list."""
    for p in list(state.placements):
        state.remove(p)
    for p in placements:
        state.add(p.node_id, p.opp, p.slot)


# ── Stage C: per-node sequencing ─────────────────────────────────────────────

def _side_of_meridian(az_deg: float) -> str:
    return "E" if (az_deg % 360.0) < 180.0 else "W"


def needs_meridian_flip(mount_type: str, az_from: float, az_to: float) -> bool:
    """An equatorial mount must flip when consecutive targets straddle the
    south meridian (east side → west side)."""
    if str(mount_type) != "equatorial":
        return False
    return _side_of_meridian(az_from) != _side_of_meridian(az_to)


def sequence_node(ctx: NodeContext, assigned: list, coord: dict,
                  net_groups: dict) -> list:
    """Order one node's committed observations in time and emit PlanItems with
    slew/filter/flip overhead reflected in the explanation."""
    assigned = sorted(assigned, key=lambda os: os[1])
    items: list = []
    prev = None
    for opp, slot in assigned:
        start_utc = ctx.slot_utc(slot)
        start_local = start_utc + ctx.utc_offset

        overhead_s = 0.0
        flip = False
        if prev is not None:
            sep = angular_separation_deg(prev.ra_deg, prev.dec_deg, opp.ra_deg, opp.dec_deg)
            az_from = prev.az_by_slot.get(prev_slot, 0.0)
            az_to = opp.az_by_slot.get(slot, 0.0)
            flip = needs_meridian_flip(ctx.mount_type, az_from, az_to)
            overhead_s = objective.transition_overhead_seconds(
                sep, prev.filter != opp.filter, flip, coord)

        # Coordination rationale for this observation.
        group = net_groups.get(opp.target_id, [])
        rank = next((i for i, g in enumerate(group)
                     if g.node_id == opp.node_id and g.slot == slot), 0)
        lon_sep = min((_longitude_sep(opp.longitude, g.opp.longitude)
                       for g in group if not (g.node_id == opp.node_id and g.slot == slot)),
                      default=0.0)

        explanation = dict(opp.components.get("explanation") or {})
        explanation.update({
            "scheduled_start_utc": start_utc.isoformat(),
            "scheduled_start_local": start_local.strftime("%H:%M"),
            "duration_minutes": opp.duration_min,
            "slot_quality": round(opp.feasible.get(slot, 1.0), 3),
            "network_redundancy_rank": rank,           # 0 = first network obs tonight
            "longitude_separation_deg": round(lon_sep, 1),
            "transition_overhead_s": round(overhead_s, 1),
            "meridian_flip": flip,
            "marginal_value": round(
                opp.base_value * opp.feasible.get(slot, 1.0)
                * objective.redundancy_factor(rank, lon_sep, coord), 4),
        })

        items.append(PlanItem(
            target=opp.name,
            ra=round(opp.ra_deg / 15.0, 4),
            dec=round(opp.dec_deg, 4),
            expDur=opp.exp_dur,
            expCount=opp.exp_count,
            binning=1,
            startTime=start_local.strftime("%H:%M"),
            target_id=opp.target_id,
            score=round(opp.base_value, 4),
            filter=opp.filter,
            notes=f"type={opp.target_type} mag={opp.mag} "
                  f"rank={rank} q={opp.feasible.get(slot, 1.0):.2f}",
            explanation=explanation,
            observation_mode=opp.observation_mode,
            duration_minutes=opp.duration_min if opp.observation_mode == "time_series" else 0.0,
        ))
        prev, prev_slot = opp, slot

    items.sort(key=lambda i: i.startTime)
    return items


# ── Orchestration ────────────────────────────────────────────────────────────

def _plan(config: dict, nodes: list) -> tuple:
    """Build, assign, sequence and persist plans for the given nodes.
    Returns (plans_by_node, stats)."""
    params = tuning.active_params(config)

    contexts: dict = {}
    opps_by_node: dict = {}
    for node in nodes:
        if node.get("status") == "disabled":
            continue
        try:
            ctx = build_node_context(node, config)
            if ctx is None:
                continue
            contexts[ctx.node_id] = ctx
            opps_by_node[ctx.node_id] = build_opportunities(ctx, config, params)
        except Exception as exc:
            logger.error("Opportunity build failed for %s: %s", node["node_id"], exc)
            incidents.log(node["node_id"], "plan_generation_failed",
                          severity="error", detail={"error": str(exc)})

    if not contexts:
        return {}, {"n_assignments": 0, "final_objective": 0.0, "greedy_objective": 0.0}

    assignments, stats = assign_network(contexts, opps_by_node, config, params)

    coord = params.get("coordination", objective.DEFAULT_COORD_PARAMS)
    # Group all placements by target for cross-node rationale in sequencing.
    all_placements = [_Placement(nid, opp, slot)
                      for nid, lst in assignments.items() for opp, slot in lst]
    net_groups = _target_groups(all_placements, contexts)

    plans_by_node: dict = {}
    for node_id, ctx in contexts.items():
        items = sequence_node(ctx, assignments.get(node_id, []), coord, net_groups)
        night_local = (ctx.t0 + ctx.utc_offset).strftime("%Y-%m-%d")
        plan = ObservationPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:10]}",
            node_id=node_id,
            night=night_local,
            generated_at=_now(),
            items=items,
        )
        _save_plan(plan)
        plans_by_node[node_id] = plan
        logger.info("Plan %s for %s: %d targets", plan.plan_id, node_id, len(items))

    _record_run(config, contexts, all_placements, stats)
    return plans_by_node, stats


def plan_network(config: dict) -> int:
    """Generate fresh coordinated plans for the whole fleet. Returns plan count."""
    nodes = registry.list_nodes()
    plans_by_node, stats = _plan(config, nodes)
    logger.info("Network plan: %d nodes, %d assignments, objective %.3f (greedy %.3f)",
                len(plans_by_node), stats.get("n_assignments", 0),
                stats.get("final_objective", 0.0), stats.get("greedy_objective", 0.0))
    return len(plans_by_node)


def plan_single_node(node: dict, config: dict) -> Optional[ObservationPlan]:
    """On-demand plan for one node (no cross-node coordination available).
    Returns the plan, or None when the node is not observing tonight."""
    plans_by_node, _ = _plan(config, [node])
    return plans_by_node.get(node["node_id"])


def _record_run(config, contexts, placements, stats) -> None:
    """Persist a telemetry row summarizing the network run (for admin + AI
    tuning evidence). Best-effort — never fails plan generation."""
    try:
        groups = _target_groups(placements, contexts)
        n_targets = len(groups)
        n_obs = len(placements)
        redundancy_rate = round(n_obs / n_targets, 3) if n_targets else 0.0
        cadence_fill = _cadence_fill(groups, contexts)
        db.execute(
            """INSERT INTO plan_runs
                   (run_id, ran_at, n_nodes, n_targets, n_assignments,
                    objective_value, greedy_objective, redundancy_rate, cadence_fill, stats)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (f"run_{uuid.uuid4().hex[:10]}", _now(), len(contexts), n_targets, n_obs,
             stats.get("final_objective", 0.0), stats.get("greedy_objective", 0.0),
             redundancy_rate, cadence_fill, json.dumps(stats)),
        )
    except Exception as exc:
        logger.warning("plan_runs telemetry write failed: %s", exc)


def _cadence_fill(groups: dict, contexts: dict) -> float:
    """Mean fraction of a target's desired cadence bins that got filled across
    the night-cycle (1.0 = every target sampled at its full cadence)."""
    if not groups:
        return 0.0
    fills = []
    for ps in groups.values():
        if len(ps) < 2:
            fills.append(1.0 if ps else 0.0)
            continue
        times = sorted(contexts[p.node_id].slot_utc(p.slot) for p in ps)
        cadence_h = max(float(ps[0].opp.cadence_hours or 24.0), 1e-6)
        span_h = (times[-1] - times[0]).total_seconds() / 3600.0
        wanted = max(1.0, span_h / cadence_h)
        fills.append(objective.clamp01(len(times) / (wanted + 1.0)))
    return round(sum(fills) / len(fills), 3)
