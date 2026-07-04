#!/usr/bin/env python3
"""
CHORUS T2 — opportunity generation and fleet-wide assignment (CHORUS.md §2, §6).

The objective is expected captured cell value:

    Φ(A) = Σ_cells ν_u · (1 − r_u(A))  +  exploration
    r_u(A) = Π_{o∈A} (1 − p_o · ρ(u, o))

with p_o = p_sky(slot) · p_exec(node) · p_accept(node) and
ρ(u,o) = g(σ_o, σ_ref(u)) · kernel(u, o).  Each cell term is a probabilistic
coverage function → Φ is monotone submodular, so the solver is a lazy
cost-benefit greedy (exact under submodularity) followed by a seeded,
time-boxed Metropolis local search that never returns worse than the greedy.

Every hand-tuned coordination mechanism of the old optimizer (redundancy
decay, cadence bonus, longitude diversity, robustness relax, trust multiplier,
telescope match) is emergent here — the only coupling between nodes is that
they draw down the same residual ledger R.

The solver core (assign, marginal, phi) is pure and DB-free for unit testing;
build_opportunities is the DB/forecast-facing generator.
"""

import heapq
import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from cloud.chorus import cells as cellmod
from cloud.chorus import physics

logger = logging.getLogger("cloud.chorus.assign")

STEP_MIN = 15                      # slot granularity (matches the fleet)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class SlotEval:
    p_sky: float                   # calibrated clear probability over the dwell
    sigma: float                   # predicted stacked error (mag) at this slot
    alt: float
    az: float


@dataclass
class ChorusOpportunity:
    node_id: str
    target_id: str
    name: str
    ra_deg: float
    dec_deg: float
    mag: Optional[float]
    target_type: str
    exposure: physics.ExposurePlan
    need: int                      # occupancy in slots
    slots: dict                    # start slot -> SlotEval
    filter: str
    p_exec: float
    p_accept: float
    explore: float                 # node exploration scalar (posterior width)
    node_lon: float
    ephemeris: Optional[dict] = None
    variant: str = "epoch"         # "epoch" | "transit_full" | "transit_core"
    observation_mode: str = "single_epoch"
    duration_min: float = 0.0
    seq: int = 0                   # deterministic tiebreak

    @property
    def key(self) -> tuple:
        return (self.node_id, self.target_id, self.variant)


@dataclass
class Placement:
    node_id: str
    opp: ChorusOpportunity
    slot: int
    marginal: float = 0.0          # exact marginal value at commit time
    p: float = 0.0                 # delivery probability used
    touches: list = field(default_factory=list)   # [(cell, rho)] at commit


# ── Capture geometry ──────────────────────────────────────────────────────────

def _dwell_bounds(opp: ChorusOpportunity, slot: int, ctx) -> tuple:
    start = ctx.slot_utc(slot)
    dwell = opp.duration_min or opp.exposure.dwell_min
    return start, start + timedelta(minutes=dwell)


def touches(opp: ChorusOpportunity, slot: int, ctx, cell_list: list,
            params: dict) -> list:
    """[(cell, rho)] for this placement — the cells it would draw down."""
    if slot not in opp.slots:
        return []
    start, end = _dwell_bounds(opp, slot, ctx)
    sigma = opp.slots[slot].sigma
    out = []
    for cell in cell_list:
        k = cellmod.kernel(cell, start, end, opp.filter, opp.ephemeris, params)
        if k < 1e-4:
            continue
        rho = physics.capture(sigma, cell.sigma_ref) * k
        if rho >= 1e-4:
            out.append((cell, rho))
    return out


def delivery_p(opp: ChorusOpportunity, slot: int) -> float:
    se = opp.slots[slot]
    return max(0.0, min(1.0, se.p_sky * opp.p_exec * opp.p_accept))


# ── Solver state ──────────────────────────────────────────────────────────────

class _State:
    """Mutable assignment state: occupancy grids, the residual ledger R, the
    same-site touch set, per-node/target counters."""

    def __init__(self, contexts: dict, cells_by_target: dict,
                 same_site_factor: float = 0.25):
        self.contexts = contexts
        self.same_site_factor = same_site_factor
        self.free = {nid: [True] * ctx.n_slots for nid, ctx in contexts.items()}
        self.node_count = {nid: 0 for nid in contexts}
        self.target_count: dict = {}
        self.R = {c.cell_id: max(0.0, min(1.0, c.residual))
                  for cl in cells_by_target.values() for c in cl}
        self.site_touched: set = set()     # (cell_id, node_id)
        self.placed_keys: set = set()
        self.placements: list = []

    def slots_free(self, node_id: str, slot: int, need: int) -> bool:
        f = self.free[node_id]
        if slot < 0 or slot + need > len(f):
            return False
        return all(f[slot:slot + need])

    def commit(self, p: Placement) -> None:
        for s in range(p.slot, p.slot + p.opp.need):
            self.free[p.node_id][s] = False
        self.node_count[p.node_id] += 1
        self.target_count[p.opp.target_id] = \
            self.target_count.get(p.opp.target_id, 0) + 1
        self.placed_keys.add(p.opp.key)
        self.placements.append(p)
        for cell, rho in p.touches:
            eff = p.p * rho
            if (cell.cell_id, p.node_id) in self.site_touched:
                eff *= self.same_site_factor
            self.R[cell.cell_id] *= (1.0 - eff)
            self.site_touched.add((cell.cell_id, p.node_id))


def marginal(opp: ChorusOpportunity, slot: int, state: _State,
             cells_by_target: dict, params: dict) -> tuple:
    """Exact Δ(o | A) at one slot: cell gains against the current residuals,
    with the same-site weather-correlation cap, plus the exploration bonus.
    Returns (value, touches)."""
    ctx = state.contexts[opp.node_id]
    tl = touches(opp, slot, ctx, cells_by_target.get(opp.target_id, []), params)
    p = delivery_p(opp, slot)
    ssf = float(params.get("same_site_repeat_factor", 0.25))
    gain = 0.0
    for cell, rho in tl:
        contrib = cell.nu * state.R.get(cell.cell_id, 1.0) * p * rho
        if (cell.cell_id, opp.node_id) in state.site_touched:
            # A same-site repeat cannot harvest weather-survival (§4.3).
            contrib *= ssf
        gain += contrib
    beta = float(params.get("exploration_beta", 0.15))
    k = state.node_count[opp.node_id]
    gain += beta * opp.explore * (0.5 ** k)
    return gain, tl


def best_slot(opp: ChorusOpportunity, state: _State, cells_by_target: dict,
              params: dict) -> tuple:
    """(slot, value, touches) of the best free start slot, or (None, 0, [])."""
    best = (None, 0.0, [])
    for s in sorted(opp.slots.keys()):
        if not state.slots_free(opp.node_id, s, opp.need):
            continue
        v, tl = marginal(opp, s, state, cells_by_target, params)
        if v > best[1] + 1e-12:
            best = (s, v, tl)
    return best


def optimistic_value(opp: ChorusOpportunity, cells_by_target: dict,
                     params: dict, ctx) -> float:
    """Upper bound on Δ (all residuals 1, best slot, no occupancy) — the lazy
    heap's initial key and the generation-time prefilter."""
    cell_list = cells_by_target.get(opp.target_id, [])
    if not cell_list or not opp.slots:
        return 0.0
    best = 0.0
    for s in sorted(opp.slots.keys()):
        p = delivery_p(opp, s)
        if p <= 0:
            continue
        v = sum(cell.nu * max(0.0, min(1.0, cell.residual)) * p * rho
                for cell, rho in touches(opp, s, ctx, cell_list, params))
        if v > best:
            best = v
    beta = float(params.get("exploration_beta", 0.15))
    return best + beta * opp.explore


# ── Φ (for stats and the local search) ────────────────────────────────────────

def phi(placements: list, cells_by_target: dict, contexts: dict,
        params: dict) -> float:
    """Order-independent expected captured value of an assignment, recomputed
    from scratch (used by telemetry and local-search move evaluation)."""
    R: dict = {}
    base: dict = {}
    for cl in cells_by_target.values():
        for c in cl:
            base[c.cell_id] = c
            R[c.cell_id] = max(0.0, min(1.0, c.residual))
    site_seen: set = set()
    ssf = float(params.get("same_site_repeat_factor", 0.25))
    total = 0.0
    node_counts: dict = {}
    # Deterministic order: by (node, slot, target).
    ordered = sorted(placements,
                     key=lambda p: (p.node_id, p.slot, p.opp.target_id))
    for p in ordered:
        ctx = contexts[p.node_id]
        tl = touches(p.opp, p.slot, ctx,
                     cells_by_target.get(p.opp.target_id, []), params)
        pr = delivery_p(p.opp, p.slot)
        for cell, rho in tl:
            eff = pr * rho
            contrib = cell.nu * R[cell.cell_id] * eff
            if (cell.cell_id, p.node_id) in site_seen:
                contrib *= ssf
                eff *= ssf
            total += contrib
            R[cell.cell_id] *= (1.0 - eff)
            site_seen.add((cell.cell_id, p.node_id))
        node_counts[p.node_id] = node_counts.get(p.node_id, 0) + 1
    beta = float(params.get("exploration_beta", 0.15))
    for nid, k in node_counts.items():
        # Σ_{i=0..k-1} 0.5^i · explore — order-independent closed form.
        explore = next((p.opp.explore for p in ordered if p.node_id == nid), 0.0)
        total += beta * explore * 2.0 * (1.0 - 0.5 ** k)
    return total


# ── Lazy greedy + seeded local search ─────────────────────────────────────────

def assign(contexts: dict, opps_by_node: dict, cells_by_target: dict,
           params: dict, seed: int = 1234,
           local_search_ms: float = 1500.0) -> tuple:
    """Fleet-wide assignment maximizing Φ.  Returns (placements, R, stats).

    Lazy greedy: a max-heap of optimistic marginals; each pop is re-evaluated
    exactly against the current residual ledger and committed only if it still
    beats the next key (exact under submodularity — marginals only shrink).
    Then a seeded Metropolis local search relocates/drops/adds within the
    time budget, keeping the best Φ seen.
    """
    eps = float(params.get("min_marginal", 0.02))
    max_per_target = max(1, int(float(params.get("max_obs_per_target", 4))))
    state = _State(contexts, cells_by_target,
                   float(params.get("same_site_repeat_factor", 0.25)))

    heap: list = []
    for nid, opps in opps_by_node.items():
        ctx = contexts[nid]
        for opp in opps:
            v = optimistic_value(opp, cells_by_target, params, ctx)
            if v > eps:
                heapq.heappush(heap, (-v, opp.seq, opp))

    while heap:
        negv, seq, opp = heapq.heappop(heap)
        ctx = contexts[opp.node_id]
        if (state.node_count[opp.node_id] >= ctx.max_targets
                or state.target_count.get(opp.target_id, 0) >= max_per_target
                or opp.key in state.placed_keys):
            continue
        slot, val, tl = best_slot(opp, state, cells_by_target, params)
        if slot is None or val < eps:
            continue
        next_best = -heap[0][0] if heap else 0.0
        if val + 1e-9 < next_best:
            heapq.heappush(heap, (-val, seq, opp))
            continue
        state.commit(Placement(node_id=opp.node_id, opp=opp, slot=slot,
                               marginal=val, p=delivery_p(opp, slot),
                               touches=tl))

    greedy_phi = phi(state.placements, cells_by_target, contexts, params)
    best_placements = list(state.placements)
    best_phi = greedy_phi

    if local_search_ms > 0 and state.placements:
        best_phi, best_placements = _local_search(
            state, opps_by_node, cells_by_target, contexts, params,
            seed, local_search_ms, greedy_phi, max_per_target)

    stats = {
        "greedy_phi": round(greedy_phi, 4),
        "final_phi": round(best_phi, 4),
        "n_assignments": len(best_placements),
        "expected_deliveries": round(sum(p.p for p in best_placements), 2),
    }
    return best_placements, state.R, stats


def replay(contexts: dict, cells_by_target: dict, placements: list,
           params: dict) -> _State:
    """Fresh solver state with `placements` re-committed in deterministic
    order, recomputing each marginal against the ledger as it replays —
    gives downstream consumers (sequencing explanations, contingency ladders,
    incremental repair) a consistent residual ledger and honest per-item
    expected-information values even for placements the local search moved."""
    st = _State(contexts, cells_by_target,
                float(params.get("same_site_repeat_factor", 0.25)))
    for p in sorted(placements,
                    key=lambda p: (p.node_id, p.slot, p.opp.target_id)):
        val, tl = marginal(p.opp, p.slot, st, cells_by_target, params)
        st.commit(Placement(node_id=p.node_id, opp=p.opp, slot=p.slot,
                            marginal=val, p=delivery_p(p.opp, p.slot),
                            touches=tl))
    return st


def _local_search(state: _State, opps_by_node: dict, cells_by_target: dict,
                  contexts: dict, params: dict, seed: int, budget_ms: float,
                  greedy_phi: float, max_per_target: int) -> tuple:
    """Seeded Metropolis over relocate / drop / add moves on a candidate
    placement list, scored by full Φ recomputation.  Never returns a solution
    worse than the greedy seed."""
    rng = random.Random(seed)
    deadline = time.monotonic() + budget_ms / 1000.0
    all_opps = [o for lst in opps_by_node.values() for o in lst]
    cur = list(state.placements)
    cur_phi = greedy_phi
    best, best_phi = list(cur), greedy_phi
    temp = 0.03
    it = 0
    while time.monotonic() < deadline and it < 4000:
        it += 1
        cand = list(cur)
        move = rng.random()
        if move < 0.40 and cand:                     # relocate
            i = rng.randrange(len(cand))
            p = cand[i]
            alts = [s for s in p.opp.slots if s != p.slot]
            if not alts:
                continue
            cand[i] = Placement(node_id=p.node_id, opp=p.opp,
                                slot=rng.choice(alts))
        elif move < 0.60 and cand:                   # drop
            cand.pop(rng.randrange(len(cand)))
        else:                                        # add
            placed = {p.opp.key for p in cand}
            pool = [o for o in all_opps if o.key not in placed]
            if not pool:
                continue
            o = pool[rng.randrange(len(pool))]
            slots = sorted(o.slots.keys())
            if not slots:
                continue
            cand.append(Placement(node_id=o.node_id, opp=o,
                                  slot=rng.choice(slots)))
        if not _feasible(cand, contexts, max_per_target):
            continue
        cand_phi = phi(cand, cells_by_target, contexts, params)
        delta = cand_phi - cur_phi
        if delta >= 0 or rng.random() < math.exp(delta / max(temp, 1e-6)):
            cur, cur_phi = cand, cand_phi
            if cand_phi > best_phi:
                best, best_phi = list(cand), cand_phi
        temp *= 0.995
    return best_phi, best


def _feasible(placements: list, contexts: dict, max_per_target: int) -> bool:
    """Occupancy / capacity check for a candidate placement list."""
    free = {nid: [True] * ctx.n_slots for nid, ctx in contexts.items()}
    node_count: dict = {}
    target_count: dict = {}
    for p in placements:
        ctx = contexts.get(p.node_id)
        if ctx is None or p.slot < 0 or p.slot + p.opp.need > ctx.n_slots:
            return False
        if p.slot not in p.opp.slots:
            return False
        for s in range(p.slot, p.slot + p.opp.need):
            if not free[p.node_id][s]:
                return False
            free[p.node_id][s] = False
        node_count[p.node_id] = node_count.get(p.node_id, 0) + 1
        if node_count[p.node_id] > ctx.max_targets:
            return False
        target_count[p.opp.target_id] = target_count.get(p.opp.target_id, 0) + 1
        if target_count[p.opp.target_id] > max_per_target:
            return False
    return True


# ── Opportunity generation (DB / forecast facing) ─────────────────────────────

def _calibrated_p_sky(raw_clear: Optional[float], cal: Optional[dict]) -> float:
    """Per-site logistic recalibration of the raw forecast clear fraction
    (Ring 0).  Identity mapping until the site has enough history."""
    if raw_clear is None:
        return 0.5
    c = max(1e-4, min(1.0 - 1e-4, raw_clear))
    if not cal:
        return c
    a = float(cal.get("a", 0.0))
    b = float(cal.get("b", 1.0))
    z = a + b * math.log(c / (1.0 - c))
    return 1.0 / (1.0 + math.exp(-z))


def build_opportunities(ctx, node: dict, vec: dict, site_cal: Optional[dict],
                        targets: list, cells_by_target: dict,
                        params: dict, config: dict,
                        transit_scarcity: float = 1.0,
                        ephemeris_by_target: Optional[dict] = None,
                        seq_start: int = 0) -> list:
    """All feasible CHORUS opportunities for one node tonight.

    ctx — network_planner.NodeContext (reused: dark window, horizon mask,
    filters, capacity); vec — the node's ledger vector (p_exec, p_accept,
    kappa, explore); site_cal — Ring-0 weather calibration; cells_by_target —
    the shared cell registry (transit event cells are registered here so every
    node sees the same cells).
    """
    from cloud.conditions import (
        altaz_curve, angular_separation_deg, astro_cloud_cover_at,
        cloud_cover_at, fetch_astronomy_weather, fetch_weather,
        horizon_min_alt, moon_state,
    )
    from cloud.network_planner import _pick_filter
    from cloud.transit_windows import get_tonight_transits

    astro = fetch_astronomy_weather(ctx.lat, ctx.lon)
    generic = fetch_weather(ctx.lat, ctx.lon) if astro is None else None

    def clear_at(when):
        cc = (astro_cloud_cover_at(astro, when) if astro is not None
              else cloud_cover_at(generic, when))
        return None if cc is None else max(0.0, min(1.0, 1.0 - cc))

    mid = ctx.t0 + (ctx.t1 - ctx.t0) / 2
    moon_samples = [(ctx.t0, moon_state(ctx.t0)), (mid, moon_state(mid)),
                    (ctx.t1, moon_state(ctx.t1))]

    def moon_at(when):
        return min(moon_samples,
                   key=lambda ms: abs((ms[0] - when).total_seconds()))[1]

    p_sky_by_slot = [
        _calibrated_p_sky(clear_at(ctx.slot_utc(s)), site_cal)
        for s in range(ctx.n_slots)
    ]

    p_exec = float(vec.get("p_exec", 0.6))
    p_accept = float(vec.get("p_accept", 0.6))
    kappa = float(vec.get("kappa", 1.0))
    explore = float(vec.get("explore", 0.0))
    mpsas = float(node.get("light_pollution_mpsas", 20.0) or 20.0)
    bright_limit = float(node.get("mag_bright_limit", 6.0) or 6.0)
    eps = float(params.get("min_marginal", 0.02))

    opps: list = []
    seq = seq_start

    for target in targets:
        cell_list = cells_by_target.get(target["target_id"]) or []
        if not cell_list:
            continue
        mag = target.get("mag")
        if mag is not None and float(mag) < bright_limit - 1.0:
            continue        # saturation: hard feasibility gate

        curve = altaz_curve(target["ra_deg"], target["dec_deg"],
                            ctx.lat, ctx.lon, ctx.t0, ctx.t1, step_min=STEP_MIN)
        alts = [c[1] for c in curve]
        azs = [c[2] for c in curve]
        clear_slots = []
        for s in range(min(ctx.n_slots, len(alts))):
            req = max(ctx.min_alt, horizon_min_alt(ctx.horizon_mask, azs[s]))
            if alts[s] >= req:
                clear_slots.append(s)
        if not clear_slots:
            continue

        best_alt_slot = max(clear_slots, key=lambda s: alts[s])
        moon_best = moon_at(ctx.slot_utc(best_alt_slot))
        moon_sep = angular_separation_deg(
            target["ra_deg"], target["dec_deg"],
            moon_best["ra_deg"], moon_best["dec_deg"])
        sigma_ref_min = min(c.sigma_ref for c in cell_list)
        exposure = physics.best_exposure(
            node, mag, alts[best_alt_slot], sigma_ref_min, mpsas=mpsas,
            moon_illum=moon_best["illumination"], moon_sep_deg=moon_sep,
            ra_deg=target["ra_deg"], dec_deg=target["dec_deg"], kappa=kappa)
        need = max(1, math.ceil(
            (exposure.dwell_min + physics.SLEW_RESERVE_MIN) / STEP_MIN))
        if need > ctx.n_slots:
            continue

        clear_set = set(clear_slots)
        slots: dict = {}
        for s in clear_slots:
            if s + need > ctx.n_slots:
                continue
            if not all((s + k) in clear_set for k in range(need)):
                continue
            when = ctx.slot_utc(s)
            m = moon_at(when)
            sep = angular_separation_deg(target["ra_deg"], target["dec_deg"],
                                         m["ra_deg"], m["dec_deg"])
            sigma = physics.sigma_total(
                node, mag, alts[s], exposure.t_sub, exposure.n_sub,
                mpsas=mpsas, moon_illum=m["illumination"], moon_sep_deg=sep,
                ra_deg=target["ra_deg"], dec_deg=target["dec_deg"], kappa=kappa)
            window = p_sky_by_slot[s:s + need]
            p_sky = sum(window) / len(window) if window else 0.5
            slots[s] = SlotEval(p_sky=p_sky, sigma=sigma,
                                alt=alts[s], az=azs[s])
        if not slots:
            continue

        seq += 1
        opp = ChorusOpportunity(
            node_id=ctx.node_id, target_id=target["target_id"],
            name=target["name"], ra_deg=target["ra_deg"],
            dec_deg=target["dec_deg"],
            mag=(float(mag) if mag is not None else None),
            target_type=target.get("target_type", "unknown"),
            exposure=exposure, need=need, slots=slots,
            filter=_pick_filter(target, ctx.filters),
            p_exec=p_exec, p_accept=p_accept, explore=explore,
            node_lon=ctx.lon,
            ephemeris=(ephemeris_by_target or {}).get(target["target_id"]),
            duration_min=exposure.dwell_min, seq=seq)
        if optimistic_value(opp, cells_by_target, params, ctx) > eps:
            opps.append(opp)

    # ── Transit opportunities: pinned time-series over event sub-windows ──────
    try:
        windows = get_tonight_transits(ctx.t0, ctx.t1, lat_deg=ctx.lat,
                                       lon_deg=ctx.lon, min_alt_deg=ctx.min_alt)
    except Exception as exc:
        logger.warning("Transit lookup failed for %s: %s", ctx.node_id, exc)
        windows = []

    for tw in windows:
        # Event cells are global (defined by the event, not the node) —
        # registered once, shared by every node that can see the transit.
        if tw.target_id not in cells_by_target:
            half = timedelta(hours=max(tw.duration_hours, 0.2) / 2.0)
            baseline = timedelta(minutes=45)
            cells_by_target[tw.target_id] = cellmod.transit_cells(
                tw.target_id, tw.name, tw.t_mid_utc, tw.duration_hours,
                tw.depth_ppt, tw.t_mid_utc - half - baseline,
                tw.t_mid_utc + half + baseline, params, transit_scarcity)

        for variant, w0, w1 in _transit_variants(tw):
            s0 = int((w0 - ctx.t0).total_seconds() / 60 / STEP_MIN)
            obs_min = (w1 - w0).total_seconds() / 60.0
            need = max(1, math.ceil(obs_min / STEP_MIN))
            if s0 < 0 or s0 + need > ctx.n_slots:
                continue
            t_sub = min(float(node.get("max_exposure_s", 30.0) or 30.0), 30.0)
            bin_subs = max(3, int(600.0 / t_sub))     # σ per ~10-min bin
            mid_slot = s0 + need // 2
            alt = alts_at(ctx, tw.ra_deg, tw.dec_deg, mid_slot)
            m = moon_at(ctx.slot_utc(mid_slot))
            sep = angular_separation_deg(tw.ra_deg, tw.dec_deg,
                                         m["ra_deg"], m["dec_deg"])
            sigma = physics.sigma_total(
                node, tw.mag, alt, t_sub, bin_subs, mpsas=mpsas,
                moon_illum=m["illumination"], moon_sep_deg=sep,
                ra_deg=tw.ra_deg, dec_deg=tw.dec_deg, kappa=kappa)
            window = p_sky_by_slot[s0:s0 + need]
            p_sky = sum(window) / len(window) if window else 0.5
            seq += 1
            opps.append(ChorusOpportunity(
                node_id=ctx.node_id, target_id=tw.target_id, name=tw.name,
                ra_deg=tw.ra_deg, dec_deg=tw.dec_deg, mag=tw.mag,
                target_type="EXOPLANET",
                exposure=physics.ExposurePlan(
                    t_sub=t_sub, n_sub=max(10, int(obs_min * 60 / t_sub)),
                    dwell_min=round(obs_min, 1), sigma=round(sigma, 5)),
                need=need,
                slots={s0: SlotEval(p_sky=p_sky, sigma=sigma, alt=alt, az=0.0)},
                filter=(ctx.filters[0] if ctx.filters else "CV"),
                p_exec=p_exec, p_accept=p_accept, explore=explore,
                node_lon=ctx.lon, variant=variant,
                observation_mode="time_series",
                duration_min=round(obs_min, 1), seq=seq))
    return opps


def _transit_variants(tw) -> list:
    """(variant, start, end) windows: the full recommended window and a
    core-only fallback (ingress→egress ±15 min) so partial coverage by a
    second node composes instead of being all-or-nothing."""
    half = timedelta(hours=max(tw.duration_hours, 0.2) / 2.0)
    pad = timedelta(minutes=15)
    out = [("transit_full", tw.obs_start_utc, tw.obs_end_utc)]
    core = (tw.t_mid_utc - half - pad, tw.t_mid_utc + half + pad)
    if core[0] > tw.obs_start_utc + timedelta(minutes=10):
        out.append(("transit_core", core[0], core[1]))
    return out


def alts_at(ctx, ra_deg: float, dec_deg: float, slot: int) -> float:
    """Altitude of a target at one slot (used for transit σ evaluation)."""
    from cloud.conditions import target_alt
    return target_alt(ra_deg, dec_deg, ctx.lat, ctx.lon, ctx.slot_utc(slot))
