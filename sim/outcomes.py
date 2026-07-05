#!/usr/bin/env python3
"""
Outcome realization — what actually happens to a planned observation.

This is the only module that reads the truth layer (SimNode.p_exec_true,
kappa_true, weather truth).  The causal chain mirrors production:

  plan item → node online tonight?          Bernoulli(p_night_up), per node
            → sky clear over the dwell?     weather TRUTH (not forecast)
            → node executed & delivered?    Bernoulli(p_exec_true)
            → realized photometric error    σ_real = σ_phys·√κ_true · e^ε,
                                            ε ~ N(0, 0.12)  (frame-to-frame lottery)
            → passed QC / AAVSO gates?      Bernoulli(p_accept_true)
                                            AND σ_real ≤ qc_sigma_max
                                            AND not a catalog/comparison failure

All draws are seeded on (seed, night, node, target, slot) so two schedulers
simulated over the same world see identically-distributed — and for identical
placements, identical — outcomes.  Node-level draws (online tonight) depend
only on (seed, night, node): a node is up or down regardless of which
scheduler is asking.
"""

import math
from dataclasses import dataclass
from typing import Optional

from sim.world import sub_rng

SIGMA_LOTTERY_SD = 0.12          # lognormal frame-quality scatter on σ
SINGLE_EPOCH_MIN_CLEAR = 0.75    # dwell fraction that must be clear to succeed
TIME_SERIES_MIN_CLEAR = 0.25     # a transit ride-through tolerates gaps
DEFAULT_QC_SIGMA_MAX = 0.25      # measurements noisier than this fail QC


@dataclass
class MeasurementOutcome:
    night: int
    node_id: str
    target_id: str
    name: str
    target_type: str
    slot_utc: str                # ISO start
    dwell_min: float
    mode: str                    # single_epoch | time_series
    filter: str
    node_up: bool
    weather_ok: bool
    clear_frac: float            # truth clear fraction over the dwell
    executed: bool               # data delivered
    accepted: bool               # passed QC / AAVSO-submittable
    sigma_pred: float            # what the planner predicted (belief κ folded in)
    sigma_realized: Optional[float]
    variant: str = "epoch"
    is_transit: bool = False


def node_up_tonight(seed: int, night: int, node) -> bool:
    return sub_rng(seed, "up", night, node.node_id).random() < node.p_night_up


def realize_placements(seed: int, night: int, placements: list,
                       contexts: dict, sim_nodes: dict, wx_by_node: dict,
                       kappa_belief: dict, *,
                       qc_sigma_max: float = DEFAULT_QC_SIGMA_MAX,
                       catalog_failure_prob: float = 0.0) -> list:
    """Realize one night's placements (any scheduler's) against the truth.

    placements — cloud.chorus.assign.Placement list;
    sim_nodes — node_id → SimNode (truth layer);
    wx_by_node — node_id → sim.weather.NightWeather (truth layer);
    kappa_belief — node_id → the κ the planner used inside σ_pred, so the
    truth κ can be swapped in:  σ_real = σ_pred · √(κ_true/κ_belief) · e^ε.
    """
    up_cache = {nid: node_up_tonight(seed, night, n)
                for nid, n in sim_nodes.items()}
    out = []
    for p in sorted(placements,
                    key=lambda p: (p.node_id, p.slot, p.opp.target_id)):
        opp = p.opp
        node = sim_nodes[p.node_id]
        wx = wx_by_node[p.node_id]
        truth = wx.truth_clear
        s0, s1 = p.slot, min(p.slot + opp.need, len(truth))
        window = truth[s0:s1] or [False]
        clear_frac = sum(1.0 for c in window if c) / len(window)
        mode = opp.observation_mode
        min_clear = (TIME_SERIES_MIN_CLEAR if mode == "time_series"
                     else SINGLE_EPOCH_MIN_CLEAR)
        weather_ok = clear_frac >= min_clear
        up = up_cache[p.node_id]

        rng = sub_rng(seed, "outcome", night, p.node_id, opp.target_id,
                      opp.variant, p.slot)
        exec_draw = rng.random()
        accept_draw = rng.random()
        catalog_draw = rng.random()
        eps = rng.gauss(0.0, SIGMA_LOTTERY_SD)

        executed = up and weather_ok and exec_draw < node.p_exec_true
        sigma_pred = opp.slots[p.slot].sigma
        sigma_real = None
        accepted = False
        if executed:
            kb = max(kappa_belief.get(p.node_id, 1.0), 0.01)
            sigma_real = (sigma_pred * math.sqrt(node.kappa_true / kb)
                          * math.exp(eps))
            accepted = (accept_draw < node.p_accept_true
                        and sigma_real <= qc_sigma_max
                        and catalog_draw >= catalog_failure_prob)

        ctx = contexts[p.node_id]
        out.append(MeasurementOutcome(
            night=night, node_id=p.node_id, target_id=opp.target_id,
            name=opp.name, target_type=opp.target_type,
            slot_utc=ctx.slot_utc(p.slot).isoformat(),
            dwell_min=opp.duration_min or opp.exposure.dwell_min,
            mode=mode, filter=opp.filter,
            node_up=up, weather_ok=weather_ok,
            clear_frac=round(clear_frac, 3),
            executed=executed, accepted=accepted,
            sigma_pred=round(sigma_pred, 5),
            sigma_realized=(round(sigma_real, 5) if sigma_real is not None
                            else None),
            variant=opp.variant,
            is_transit=(mode == "time_series"),
        ))
    return out
