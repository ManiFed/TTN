#!/usr/bin/env python3
"""
Scheduler adapters — every strategy consumes the SAME night bundle
(NodeContexts, ChorusOpportunities, information cells) and returns a list of
(opportunity, slot) picks.  The engine then replays all of them through the
same residual-ledger accounting and the same outcome realizer, so no
scheduler gets a friendlier world than another.

Strategies:

  chorus          the production solver, cloud.chorus.assign.assign, unchanged
  legacy          the production legacy optimizer, cloud.network_planner.
                  assign_network, unchanged — fed composite-style base values
  greedy_value    per-node independent greedy: each node takes its own
                  highest-value targets, no cross-node coordination
  greedy_nearest  per-node nearest-good-target greedy: minimizes slew by
                  hopping to the nearest target above a value floor
  random          uniform random feasible assignment (seeded) — the floor any
                  scheduler must beat

Fairness notes: the baselines see exactly the same forecasts, physics
predictions, reliability beliefs, and feasibility sets CHORUS sees.  The
legacy baseline uses the real legacy solver and objective (redundancy decay,
cadence bonus, longitude diversity), not a strawman; only its per-(target,
node) base score is reconstructed (production computes it from DB history
that does not exist in a synthetic world), following cloud/scoring.py's
composite structure.
"""

import math
import random

from cloud import objective
from cloud.chorus import assign as assign_mod
from cloud.network_planner import Opportunity as LegacyOpportunity
from cloud.network_planner import assign_network

from sim.world import sub_rng

SCHEDULERS = ("chorus", "legacy", "greedy_value", "greedy_nearest", "random")


def run_scheduler(name: str, bundle: dict, seed: int) -> list:
    """Dispatch: returns [(ChorusOpportunity, slot), ...]."""
    fn = {
        "chorus": _chorus,
        "legacy": _legacy,
        "greedy_value": _greedy_value,
        "greedy_nearest": _greedy_nearest,
        "random": _random_feasible,
    }[name]
    return fn(bundle, seed)


# ── CHORUS (production solver, unchanged) ─────────────────────────────────────

def _chorus(bundle: dict, seed: int) -> list:
    placements, _, _ = assign_mod.assign(
        bundle["contexts"], bundle["opps_by_node"], bundle["cells_by_target"],
        bundle["ch_params"], seed=seed,
        local_search_ms=bundle.get("local_search_ms", 0.0))
    return [(p.opp, p.slot) for p in placements]


# ── Legacy network optimizer (production solver, unchanged) ──────────────────

def _legacy(bundle: dict, seed: int) -> list:
    """Feed the real legacy solver.  Base value reconstructs the composite
    score's shape: priority + science value + brightness feasibility, with the
    per-slot quality array from altitude + forecast (as Stage A built it)."""
    contexts = bundle["contexts"]
    coord = dict(objective.DEFAULT_COORD_PARAMS)
    params = {"coordination": coord}
    config = {"scheduler": {"local_search_ms": 0}}

    legacy_by_node: dict = {}
    key_map: dict = {}
    for nid, opps in bundle["opps_by_node"].items():
        rows = []
        for opp in opps:
            cells = bundle["cells_by_target"].get(opp.target_id, [])
            total_nu = sum(c.nu * max(0.0, min(1.0, c.residual)) for c in cells)
            row = bundle["target_rows"].get(opp.target_id, {})
            prio = float(row.get("priority", 0.6) or 0.6)
            base = objective.clamp01(0.45 * prio
                                     + 0.45 * min(1.0, total_nu / 2.0)
                                     + 0.10)
            if opp.variant == "transit_core":
                continue        # legacy transits were all-or-nothing
            raw_q = {}
            for s, se in opp.slots.items():
                alt_q = objective.clamp01((se.alt - 20.0) / 50.0)
                raw_q[s] = 0.55 * alt_q + 0.45 * se.p_sky
            if not raw_q:
                continue
            is_transit = opp.observation_mode == "time_series"
            lop = LegacyOpportunity(
                node_id=opp.node_id, target_id=opp.target_id, name=opp.name,
                ra_deg=opp.ra_deg, dec_deg=opp.dec_deg, mag=opp.mag,
                target_type=opp.target_type,
                base_value=(max(0.85, base) if is_transit else base),
                exp_dur=opp.exposure.t_sub, exp_count=opp.exposure.n_sub,
                need=opp.need,
                feasible=objective.normalize_slot_quality(raw_q, 0.5),
                az_by_slot={s: se.az for s, se in opp.slots.items()},
                filter=opp.filter, longitude=opp.node_lon,
                cadence_hours=float(bundle["target_rows"]
                                    .get(opp.target_id, {})
                                    .get("cadence_hours", 24.0) or 24.0),
                is_transit=is_transit,
                pinned_slot=(min(opp.slots) if is_transit else None),
                duration_min=opp.duration_min,
                observation_mode=opp.observation_mode,
            )
            rows.append(lop)
            key_map[(nid, opp.target_id, is_transit)] = opp
        legacy_by_node[nid] = rows

    assignments, _ = assign_network(contexts, legacy_by_node, config,
                                    params, seed=seed)
    picks = []
    for nid, lst in assignments.items():
        for lop, slot in lst:
            opp = key_map.get((nid, lop.target_id, lop.is_transit))
            if opp is not None and slot in opp.slots:
                picks.append((opp, slot))
    return picks


# ── Simple baselines ──────────────────────────────────────────────────────────

class _Occupancy:
    """Shared feasibility bookkeeping for the naive baselines — the same
    constraints the production solvers enforce (free slots, per-node capacity,
    per-target portfolio cap)."""

    def __init__(self, contexts: dict, max_per_target: int = 4):
        self.contexts = contexts
        self.free = {nid: [True] * ctx.n_slots for nid, ctx in contexts.items()}
        self.count = {nid: 0 for nid in contexts}
        self.per_target: dict = {}
        self.placed_keys: set = set()
        self.max_per_target = max_per_target

    def can_place(self, opp, slot: int) -> bool:
        ctx = self.contexts[opp.node_id]
        if (self.count[opp.node_id] >= ctx.max_targets
                or self.per_target.get(opp.target_id, 0) >= self.max_per_target
                or opp.key in self.placed_keys
                or slot not in opp.slots
                or slot + opp.need > ctx.n_slots):
            return False
        return all(self.free[opp.node_id][slot:slot + opp.need])

    def place(self, opp, slot: int) -> None:
        for s in range(slot, slot + opp.need):
            self.free[opp.node_id][s] = False
        self.count[opp.node_id] += 1
        self.per_target[opp.target_id] = \
            self.per_target.get(opp.target_id, 0) + 1
        self.placed_keys.add(opp.key)


def _own_value(opp, bundle: dict) -> float:
    """A node's private view of an opportunity: expected captured value with
    all residuals at 1 — i.e. the score a node computes when it ignores what
    the rest of the network is doing."""
    ctx = bundle["contexts"][opp.node_id]
    return assign_mod.optimistic_value(opp, bundle["cells_by_target"],
                                       bundle["ch_params"], ctx)


def _best_own_slot(opp, occ: _Occupancy) -> int:
    """Highest p_sky·(altitude) free slot, ties to earliest — how an
    uncoordinated node picks its moment."""
    best, best_q = None, -1.0
    for s in sorted(opp.slots):
        if not occ.can_place(opp, s):
            continue
        se = opp.slots[s]
        q = se.p_sky * max(se.alt, 1.0)
        if q > best_q + 1e-12:
            best, best_q = s, q
    return best


def _greedy_value(bundle: dict, seed: int) -> list:
    occ = _Occupancy(bundle["contexts"],
                     int(bundle["ch_params"].get("max_obs_per_target", 4)))
    picks = []
    for nid in sorted(bundle["opps_by_node"]):
        ranked = sorted(bundle["opps_by_node"][nid],
                        key=lambda o: (-_own_value(o, bundle), o.seq))
        for opp in ranked:
            if occ.count[nid] >= bundle["contexts"][nid].max_targets:
                break
            slot = _best_own_slot(opp, occ)
            if slot is not None:
                occ.place(opp, slot)
                picks.append((opp, slot))
    return picks


def _greedy_nearest(bundle: dict, seed: int) -> list:
    """Start from the node's best target, then always hop to the nearest
    still-valuable target on the sky (slew-miser heuristic)."""
    occ = _Occupancy(bundle["contexts"],
                     int(bundle["ch_params"].get("max_obs_per_target", 4)))
    picks = []
    for nid in sorted(bundle["opps_by_node"]):
        pool = [o for o in bundle["opps_by_node"][nid]
                if _own_value(o, bundle) > 0.02]
        if not pool:
            continue
        current = max(pool, key=lambda o: (_own_value(o, bundle), -o.seq))
        visited: set = set()
        while current is not None:
            if occ.count[nid] >= bundle["contexts"][nid].max_targets:
                break
            visited.add(current.key)
            slot = _best_own_slot(current, occ)
            if slot is not None:
                occ.place(current, slot)
                picks.append((current, slot))
            pool = [o for o in pool
                    if o.key not in occ.placed_keys and o.key not in visited]
            if not pool:
                break
            ra0, dec0 = current.ra_deg, current.dec_deg
            current = min(pool, key=lambda o: (_ang(ra0, dec0, o.ra_deg,
                                                    o.dec_deg), o.seq))
    return picks


def _random_feasible(bundle: dict, seed: int) -> list:
    rng = sub_rng(seed, "random-sched")
    occ = _Occupancy(bundle["contexts"],
                     int(bundle["ch_params"].get("max_obs_per_target", 4)))
    all_opps = [o for nid in sorted(bundle["opps_by_node"])
                for o in bundle["opps_by_node"][nid]]
    rng.shuffle(all_opps)
    picks = []
    for opp in all_opps:
        slots = [s for s in sorted(opp.slots) if occ.can_place(opp, s)]
        if not slots:
            continue
        slot = rng.choice(slots)
        occ.place(opp, slot)
        picks.append((opp, slot))
    return picks


def _ang(ra1, dec1, ra2, dec2) -> float:
    ra1, dec1, ra2, dec2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = (math.sin(dec1) * math.sin(dec2)
         + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))
