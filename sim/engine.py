#!/usr/bin/env python3
"""
The digital-twin engine: multi-night simulation of The Telescope Net.

For one (scenario, seed) it generates a frozen synthetic world — fleet,
catalog, weather, CV outburst schedules, transient arrivals — and then runs
each requested scheduler over that identical world.  Per scheduler it
maintains the evolving state production maintains:

  * a reliability ledger of Beta posteriors per node (the scheduler's BELIEF
    about p_exec / p_accept / κ), updated only from realized outcomes — the
    truth layer is never leaked to the planner;
  * per-target science state (EB phase-coverage residuals, CV hazard clocks
    and outburst flags, transient ages) — belief again: a CV outburst only
    reshapes the value landscape after the network actually catches it.

The night pipeline mirrors cloud/chorus/planner.py stage for stage
(T0 ledger read → T1 scarcity + cells → T2 opportunities + assignment →
outcome realization standing in for the physical night), with the production
solver cores called unchanged.

Determinism: everything derives from (scenario, seed).  The seeded local
search is DISABLED by default because its time-boxing makes results depend on
machine speed; enable via scenario.local_search_ms knowing runs then vary
across hosts (but stay deterministic in iteration-capped CI-free contexts).
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from cloud.chorus import assign as assign_mod
from cloud.chorus import cells as cellmod
from cloud.chorus import horizon, physics
from cloud.chorus import params as chorus_params
from cloud.network_planner import NodeContext, STEP_MIN

from sim import outcomes as outcomes_mod
from sim import skymath, weather
from sim.schedulers import run_scheduler
from sim.world import (SimNode, SimTarget, generate_catalog, generate_fleet,
                       region_clear_prob, sub_rng)

PHASE_BINS = cellmod.PHASE_BINS
SCARCITY_NODE_SAMPLE = 25       # nodes used in the T1 sweep (approximation)
LEDGER_DECAY = 0.5 ** (1.0 / 60.0)   # 60-night half-life, as production
TRANSIENT_FADE_MAG_PER_NIGHT = 0.08
TRANSIENT_MAX_AGE_NIGHTS = 45


# ── Per-run mutable state (belief layer) ──────────────────────────────────────

class NodeBelief:
    """Beta posteriors + κ ratio — the sim twin of cloud.chorus.ledger."""

    def __init__(self):
        self.a_e, self.b_e = 4.0, 2.0        # p_exec prior mean 2/3
        self.a_a, self.b_a = 4.0, 2.0        # p_accept prior mean 2/3
        self.kappa_sum, self.kappa_n = 0.0, 0
        self.M0 = 8.0                        # κ shrinkage pseudo-count

    @staticmethod
    def _sd(a: float, b: float) -> float:
        return math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1.0)))

    def vector(self) -> dict:
        p_exec = self.a_e / (self.a_e + self.b_e)
        p_accept = self.a_a / (self.a_a + self.b_a)
        kappa = (self.M0 * 1.0 + self.kappa_sum) / (self.M0 + self.kappa_n)
        explore = self._sd(self.a_e, self.b_e) + self._sd(self.a_a, self.b_a)
        return {"p_exec": p_exec, "p_accept": p_accept,
                "kappa": max(0.25, kappa), "explore": explore}

    def decay(self) -> None:
        self.a_e *= LEDGER_DECAY; self.b_e *= LEDGER_DECAY
        self.a_a *= LEDGER_DECAY; self.b_a *= LEDGER_DECAY

    def observe(self, clear_attempt: bool, executed: bool, accepted: bool,
                sigma_ratio_sq: Optional[float]) -> None:
        if clear_attempt:
            self.a_e += 1.0 if executed else 0.0
            self.b_e += 0.0 if executed else 1.0
        if executed:
            self.a_a += 1.0 if accepted else 0.0
            self.b_a += 0.0 if accepted else 1.0
        if sigma_ratio_sq is not None:
            self.kappa_sum += min(sigma_ratio_sq, 25.0)
            self.kappa_n += 1


class TargetBeliefState:
    """The sim twin of chorus_target_state rows."""

    def __init__(self, target: SimTarget):
        self.phase_coverage = [1.0] * PHASE_BINS if target.ephemeris else None
        self.last_accepted_utc: Optional[str] = None
        self.outburst_belief = False
        self.outburst_belief_night = -999

    def as_state(self, target: SimTarget) -> dict:
        state: dict = {}
        if target.ephemeris:
            state["ephemeris"] = target.ephemeris
            state["phase_coverage"] = list(self.phase_coverage)
        if target.cv:
            state["hazard_tau_h"] = target.cv["tau_h"]
            state["outburst"] = self.outburst_belief
        if self.last_accepted_utc:
            state["last_accepted_utc"] = self.last_accepted_utc
        return state


# ── Frozen world (truth layer, shared by all schedulers) ─────────────────────

@dataclass
class World:
    seed: int
    epoch: datetime
    nights: int
    fleet: list                       # [SimNode]
    catalog: list                     # [SimTarget]
    cv_outburst: dict                 # target_id -> [bool] per night (truth)
    scenario: "object" = None

    @property
    def nodes_by_id(self) -> dict:
        return {n.node_id: n for n in self.fleet}


def build_world(scenario, seed: int) -> World:
    epoch = datetime.fromisoformat(scenario.epoch).replace(tzinfo=timezone.utc)
    fleet = generate_fleet(
        scenario.n_nodes, seed,
        regions=scenario.regions, region_weights=scenario.region_weights,
        hardware_mix=scenario.hardware_mix,
        reliability_scale=scenario.reliability_scale,
        forecast_skill_range=scenario.forecast_skill_range,
        p_night_up_range=scenario.p_night_up_range)
    if scenario.kappa_scale != 1.0:
        for n in fleet:
            n.kappa_true = round(n.kappa_true * scenario.kappa_scale, 2)
    catalog = generate_catalog(
        scenario.n_targets, seed, epoch,
        class_mix=scenario.class_mix,
        dec_bias_north=scenario.dec_bias_north,
        transient_rate_per_night=scenario.transient_rate_per_night,
        n_nights=scenario.nights,
        alert_storm_night=scenario.alert_storm_night,
        alert_storm_count=scenario.alert_storm_count)

    # CV outburst truth — one boolean track per CV, scheduler-independent.
    cv_outburst: dict = {}
    for t in catalog:
        if not t.cv:
            continue
        rng = sub_rng(seed, "cv-truth", t.target_id)
        track, remaining = [], 0
        for _ in range(scenario.nights):
            if remaining > 0:
                track.append(True)
                remaining -= 1
            elif rng.random() < t.cv["outburst_rate_per_night"]:
                track.append(True)
                remaining = t.cv["outburst_nights"] - 1
            else:
                track.append(False)
        cv_outburst[t.target_id] = track
    return World(seed=seed, epoch=epoch, nights=scenario.nights, fleet=fleet,
                 catalog=catalog, cv_outburst=cv_outburst, scenario=scenario)


# ── Night context building ────────────────────────────────────────────────────

def build_context(node: SimNode, night_date: datetime,
                  max_targets: int) -> Optional[NodeContext]:
    """NodeContext from pure math — the sim twin of
    network_planner.build_node_context."""
    scan_start = night_date + timedelta(hours=12.0 - node.lon / 15.0)
    win = skymath.night_window(node.lat, node.lon, scan_start)
    if win is None:
        return None
    t0, t1 = win
    n_slots = max(1, int((t1 - t0).total_seconds() / 60 / STEP_MIN))
    row = node.as_row()
    return NodeContext(
        node=row, node_id=node.node_id, lat=node.lat, lon=node.lon,
        t0=t0, t1=t1, n_slots=n_slots,
        utc_offset=timedelta(hours=round(node.lon / 15.0)),
        min_alt=25.0, horizon_mask=[], filters=list(row["filter_set"]),
        cooled=bool(row["cooled_camera"]), mount_type=row["mount_type"],
        cloud_relax=0.0, max_targets=max_targets)


def _current_mag(target: SimTarget, night: int, world: World,
                 truth_outburst: bool) -> Optional[float]:
    """Truth apparent magnitude tonight (CV outbursts brighten; SNe fade)."""
    mag = target.mag
    if target.cv and truth_outburst:
        mag = mag - target.cv["outburst_delta_mag"]
    if target.target_type == "SN" and target.alert_night is not None:
        age = night - target.alert_night
        mag = mag + TRANSIENT_FADE_MAG_PER_NIGHT * max(0, age - 5)
    return round(mag, 2)


def active_targets(world: World, night: int) -> list:
    """(SimTarget, truth_outburst, mag_tonight) for targets live tonight."""
    out = []
    for t in world.catalog:
        if t.alert_night is not None:
            if night < t.alert_night or night - t.alert_night > TRANSIENT_MAX_AGE_NIGHTS:
                continue
        burst = bool(world.cv_outburst.get(t.target_id, [False])[night]) \
            if t.cv else False
        mag = _current_mag(t, night, world, burst)
        if mag is not None and mag > 17.5:
            continue
        out.append((t, burst, mag))
    return out


# ── Opportunity generation (sim twin of assign.build_opportunities) ──────────

def build_sim_opportunities(ctx: NodeContext, node: SimNode, vec: dict,
                            actives: list, cells_by_target: dict,
                            ch: dict, wx: weather.NightWeather,
                            transit_events: list, seq_start: int,
                            max_candidates: int) -> list:
    row = ctx.node
    p_exec, p_accept = vec["p_exec"], vec["p_accept"]
    kappa, explore = vec["kappa"], vec["explore"]
    mpsas = row["light_pollution_mpsas"]
    bright_limit = row["mag_bright_limit"]
    eps = float(ch.get("min_marginal", 0.02))
    p_sky = wx.forecast_clear

    mid = ctx.t0 + (ctx.t1 - ctx.t0) / 2
    moon_samples = [(ctx.t0, skymath.moon_state(ctx.t0)),
                    (mid, skymath.moon_state(mid)),
                    (ctx.t1, skymath.moon_state(ctx.t1))]

    def moon_at(when):
        return min(moon_samples,
                   key=lambda ms: abs((ms[0] - when).total_seconds()))[1]

    # Cheap prefilter, then rank NODE-SPECIFICALLY, as production does (its
    # per-(target, node) scores fold in each node's physics): residual cell
    # value × this node's capture at the target's transit altitude.  Without
    # the node term every node would shortlist the same top-K targets and the
    # per-target portfolio cap would turn max_candidates into an artificial
    # network-wide ceiling.
    candidates = []
    for target, _, mag in actives:
        cl = cells_by_target.get(target.target_id) or []
        if not cl:
            continue
        if mag is not None and mag < bright_limit - 1.0:
            continue                     # saturation gate, as production
        transit_alt = 90.0 - abs(ctx.lat - target.dec_deg)
        if transit_alt < ctx.min_alt:
            continue                     # never rises high enough
        value = sum(c.nu * max(0.0, min(1.0, c.residual)) for c in cl)
        if value <= 1e-6:
            continue
        sigma_proxy = physics.sigma_total(
            row, mag, min(transit_alt, 85.0), 30.0, 24,
            mpsas=mpsas, ra_deg=target.ra_deg, dec_deg=target.dec_deg,
            kappa=kappa)
        fit = physics.capture(sigma_proxy, min(c.sigma_ref for c in cl))
        candidates.append((value * (0.05 + fit), target, mag))
    candidates.sort(key=lambda c: (-c[0], c[1].target_id))
    candidates = candidates[:max_candidates]

    opps: list = []
    seq = seq_start
    for _, target, mag in candidates:
        cl = cells_by_target[target.target_id]
        curve = skymath.altaz_curve(target.ra_deg, target.dec_deg,
                                    ctx.lat, ctx.lon, ctx.t0, ctx.t1,
                                    step_min=STEP_MIN)
        alts = [c[1] for c in curve]
        azs = [c[2] for c in curve]
        clear_slots = [s for s in range(min(ctx.n_slots, len(alts)))
                       if alts[s] >= ctx.min_alt]
        if not clear_slots:
            continue
        best_alt_slot = max(clear_slots, key=lambda s: alts[s])
        m_best = moon_at(ctx.slot_utc(best_alt_slot))
        moon_sep = skymath.angular_separation_deg(
            target.ra_deg, target.dec_deg,
            m_best["ra_deg"], m_best["dec_deg"])
        sigma_ref_min = min(c.sigma_ref for c in cl)
        exposure = physics.best_exposure(
            row, mag, alts[best_alt_slot], sigma_ref_min, mpsas=mpsas,
            moon_illum=m_best["illumination"], moon_sep_deg=moon_sep,
            ra_deg=target.ra_deg, dec_deg=target.dec_deg, kappa=kappa)
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
            sep = skymath.angular_separation_deg(
                target.ra_deg, target.dec_deg, m["ra_deg"], m["dec_deg"])
            sigma = physics.sigma_total(
                row, mag, alts[s], exposure.t_sub, exposure.n_sub,
                mpsas=mpsas, moon_illum=m["illumination"], moon_sep_deg=sep,
                ra_deg=target.ra_deg, dec_deg=target.dec_deg, kappa=kappa)
            window = p_sky[s:s + need]
            slots[s] = assign_mod.SlotEval(
                p_sky=(sum(window) / len(window) if window else 0.5),
                sigma=sigma, alt=alts[s], az=azs[s])
        if not slots:
            continue
        seq += 1
        opp = assign_mod.ChorusOpportunity(
            node_id=ctx.node_id, target_id=target.target_id,
            name=target.name, ra_deg=target.ra_deg, dec_deg=target.dec_deg,
            mag=mag, target_type=target.target_type,
            exposure=exposure, need=need, slots=slots,
            filter=_pick_filter(target, ctx.filters),
            p_exec=p_exec, p_accept=p_accept, explore=explore,
            node_lon=ctx.lon, ephemeris=target.ephemeris,
            duration_min=exposure.dwell_min, seq=seq)
        if assign_mod.optimistic_value(opp, cells_by_target, ch, ctx) > eps:
            opps.append(opp)

    # ── Transit time-series opportunities (sim twin of the production block) ──
    for ev in transit_events:
        alt_mid, _ = skymath.altaz(ev["ra_deg"], ev["dec_deg"],
                                   ctx.lat, ctx.lon, ev["t_mid"])
        if alt_mid < ctx.min_alt:
            continue
        for variant, w0, w1 in _transit_variants(ev):
            s0 = int((w0 - ctx.t0).total_seconds() / 60 / STEP_MIN)
            obs_min = (w1 - w0).total_seconds() / 60.0
            need = max(1, math.ceil(obs_min / STEP_MIN))
            if s0 < 0 or s0 + need > ctx.n_slots:
                continue
            t_sub = min(row["max_exposure_s"], 30.0)
            bin_subs = max(3, int(600.0 / t_sub))
            mid_slot = s0 + need // 2
            alt, _ = skymath.altaz(ev["ra_deg"], ev["dec_deg"], ctx.lat,
                                   ctx.lon, ctx.slot_utc(mid_slot))
            m = moon_at(ctx.slot_utc(mid_slot))
            sep = skymath.angular_separation_deg(
                ev["ra_deg"], ev["dec_deg"], m["ra_deg"], m["dec_deg"])
            sigma = physics.sigma_total(
                row, ev["mag"], alt, t_sub, bin_subs, mpsas=mpsas,
                moon_illum=m["illumination"], moon_sep_deg=sep,
                ra_deg=ev["ra_deg"], dec_deg=ev["dec_deg"], kappa=kappa)
            window = p_sky[s0:s0 + need]
            seq += 1
            opps.append(assign_mod.ChorusOpportunity(
                node_id=ctx.node_id, target_id=ev["target_id"],
                name=ev["name"], ra_deg=ev["ra_deg"], dec_deg=ev["dec_deg"],
                mag=ev["mag"], target_type="EXOPLANET",
                exposure=physics.ExposurePlan(
                    t_sub=t_sub, n_sub=max(10, int(obs_min * 60 / t_sub)),
                    dwell_min=round(obs_min, 1), sigma=round(sigma, 5)),
                need=need,
                slots={s0: assign_mod.SlotEval(
                    p_sky=(sum(window) / len(window) if window else 0.5),
                    sigma=sigma, alt=alt, az=0.0)},
                filter=(ctx.filters[0] if ctx.filters else "CV"),
                p_exec=p_exec, p_accept=p_accept, explore=explore,
                node_lon=ctx.lon, variant=variant,
                observation_mode="time_series",
                duration_min=round(obs_min, 1), seq=seq))
    return opps


def _pick_filter(target: SimTarget, node_filters: list) -> str:
    return node_filters[0] if node_filters else "CV"


def _transit_variants(ev: dict) -> list:
    half = timedelta(hours=max(ev["duration_hours"], 0.2) / 2.0)
    pad = timedelta(minutes=15)
    baseline = timedelta(minutes=45)
    full = (ev["t_mid"] - half - baseline, ev["t_mid"] + half + baseline)
    out = [("transit_full", full[0], full[1])]
    core = (ev["t_mid"] - half - pad, ev["t_mid"] + half + pad)
    if core[0] > full[0] + timedelta(minutes=10):
        out.append(("transit_core", core[0], core[1]))
    return out


def transits_tonight(world: World, span_t0: datetime,
                     span_t1: datetime) -> list:
    """Transit events with mid-time inside tonight's fleet-wide span."""
    events = []
    for t in world.catalog:
        if not t.transit:
            continue
        tr = t.transit
        epoch = datetime.fromisoformat(tr["epoch"])
        period = timedelta(days=tr["period_days"])
        k = max(0, math.floor((span_t0 - epoch) / period))
        for kk in (k, k + 1, k + 2):
            t_mid = epoch + kk * period
            if span_t0 <= t_mid <= span_t1:
                events.append({
                    "target_id": t.target_id, "name": t.name,
                    "ra_deg": t.ra_deg, "dec_deg": t.dec_deg, "mag": t.mag,
                    "t_mid": t_mid, "duration_hours": tr["duration_hours"],
                    "depth_ppt": tr["depth_ppt"],
                    "period_days": tr["period_days"],
                })
    return events


# ── The per-scheduler run ─────────────────────────────────────────────────────

def run_world(world: World, scheduler: str, *, collect_outcomes: bool = False):
    """Simulate all nights of `world` under one scheduler.
    Returns a result dict (see sim.metrics for aggregation)."""
    sc = world.scenario
    ch = chorus_params.merged(sc.chorus_overrides)
    sim_nodes = world.nodes_by_id
    beliefs = {nid: NodeBelief() for nid in sim_nodes}
    tstate = {t.target_id: TargetBeliefState(t) for t in world.catalog}
    alert_first_accept: dict = {}
    night_records: list = []
    node_accepted: dict = {nid: 0 for nid in sim_nodes}
    all_outcomes: list = []

    for night in range(world.nights):
        night_date = world.epoch + timedelta(days=night)
        month = night_date.month
        seed_night = world.seed + night * 7919

        # ── Contexts ─────────────────────────────────────────────────────────
        contexts: dict = {}
        for node in world.fleet:
            ctx = build_context(node, night_date, sc.max_targets_per_night)
            if ctx is not None and ctx.n_slots >= 2:
                contexts[node.node_id] = ctx
        if not contexts:
            night_records.append(_empty_night(night))
            continue
        span_t0 = min(c.t0 for c in contexts.values())
        span_t1 = max(c.t1 for c in contexts.values())
        band_union = {f for c in contexts.values() for f in c.filters}

        # ── Weather (truth + forecast) ───────────────────────────────────────
        wx_by_node = {
            nid: weather.night_weather(
                world.seed, night, sim_nodes[nid], ctx.n_slots, month,
                regional_correlation=sc.regional_correlation,
                climate_overrides=sc.climate_overrides,
                forecast_skill_scale=sc.forecast_skill_scale,
                forecast_bias=sc.forecast_bias)
            for nid, ctx in contexts.items()
        }

        # ── T0 read: beliefs ─────────────────────────────────────────────────
        vecs = {nid: b.vector() for nid, b in beliefs.items()}
        p_exec_by_node = {nid: v["p_exec"] for nid, v in vecs.items()}

        # ── T1: scarcity + cells ─────────────────────────────────────────────
        actives = active_targets(world, night)
        sweep_nodes = [n.as_row() for n in world.fleet[:SCARCITY_NODE_SAMPLE]]
        region_by_id = {n.node_id: n.region for n in world.fleet}

        def clear_prob(node_row, when):
            return region_clear_prob(region_by_id[node_row["node_id"]],
                                     when.month, sc.climate_overrides)

        cells_by_target: dict = {}
        target_rows: dict = {}
        ephemeris_by_target: dict = {}
        for target, burst, mag in actives:
            if target.transit:
                continue        # exoplanets enter via event cells below
            row = target.as_row()
            row["mag"] = mag
            state = tstate[target.target_id].as_state(target)
            s = horizon.scarcity(row, sweep_nodes, p_exec_by_node,
                                 clear_prob, ch, today=span_t0)
            cl = cellmod.compile_cells(row, state, span_t0, span_t1, ch, s,
                                       band_union)
            if not cl or sum(c.nu * c.residual for c in cl) <= 1e-6:
                continue
            cells_by_target[target.target_id] = cl
            target_rows[target.target_id] = row
            if target.ephemeris:
                ephemeris_by_target[target.target_id] = target.ephemeris

        events = transits_tonight(world, span_t0, span_t1)
        for ev in events:
            if ev["target_id"] in cells_by_target:
                continue
            half = timedelta(hours=max(ev["duration_hours"], 0.2) / 2.0)
            baseline = timedelta(minutes=45)
            cells_by_target[ev["target_id"]] = cellmod.transit_cells(
                ev["target_id"], ev["name"], ev["t_mid"],
                ev["duration_hours"], ev["depth_ppt"],
                ev["t_mid"] - half - baseline, ev["t_mid"] + half + baseline,
                ch, 1.0)
            target_rows[ev["target_id"]] = {
                "target_id": ev["target_id"], "name": ev["name"],
                "target_type": "EXOPLANET", "priority": 0.9,
                "cadence_hours": ev["period_days"] * 24.0,
            }

        # ── T2: opportunities ────────────────────────────────────────────────
        mag_by_target = {t.target_id: m for t, _, m in actives}
        opps_by_node: dict = {}
        seq = 0
        for nid, ctx in contexts.items():
            opps = build_sim_opportunities(
                ctx, sim_nodes[nid], vecs[nid], actives, cells_by_target,
                ch, wx_by_node[nid], events, seq,
                sc.max_candidates_per_node)
            seq += len(opps) + 1
            opps_by_node[nid] = opps

        bundle = {
            "contexts": contexts, "opps_by_node": opps_by_node,
            "cells_by_target": cells_by_target, "ch_params": ch,
            "target_rows": target_rows,
            "local_search_ms": sc.local_search_ms,
        }
        picks = run_scheduler(scheduler, bundle, seed_night)
        _validate_picks(contexts, picks, scheduler)
        state = assign_mod.replay(
            contexts, cells_by_target,
            [assign_mod.Placement(node_id=o.node_id, opp=o, slot=s)
             for o, s in picks], ch)
        placements = state.placements

        # ── The night happens ────────────────────────────────────────────────
        night_out = outcomes_mod.realize_placements(
            world.seed, night, placements, contexts, sim_nodes, wx_by_node,
            {nid: v["kappa"] for nid, v in vecs.items()},
            qc_sigma_max=sc.qc_sigma_max,
            catalog_failure_prob=sc.catalog_failure_prob)

        # ── Realized science accounting (identical for every scheduler) ──────
        realized_value, per_cell_r = _realized_cell_value(
            night_out, cells_by_target, ephemeris_by_target, contexts, ch)

        # ── Belief updates (T0 write) ────────────────────────────────────────
        for b in beliefs.values():
            b.decay()
        for o in night_out:
            ratio_sq = None
            if o.accepted and o.sigma_realized and o.sigma_pred > 0:
                kb = max(vecs[o.node_id]["kappa"], 0.01)
                sigma_phys = o.sigma_pred / math.sqrt(kb)
                ratio_sq = (o.sigma_realized / max(sigma_phys, 1e-6)) ** 2
            beliefs[o.node_id].observe(
                clear_attempt=(o.node_up and o.weather_ok),
                executed=o.executed, accepted=o.accepted,
                sigma_ratio_sq=ratio_sq)
            if o.accepted:
                node_accepted[o.node_id] += 1

        _update_target_state(world, night, night_out, tstate,
                             ephemeris_by_target, alert_first_accept)

        night_records.append(_night_metrics(
            night, contexts, placements, night_out, cells_by_target,
            realized_value, events, wx_by_node, mag_by_target, world))
        if collect_outcomes:
            all_outcomes.extend(night_out)

    return {
        "scenario": sc.name, "scheduler": scheduler, "seed": world.seed,
        "n_nodes": len(world.fleet), "nights": night_records,
        "node_accepted": node_accepted,
        "alerts": _alert_summary(world, alert_first_accept),
        "outcomes": all_outcomes,
    }


# ── Accounting helpers ────────────────────────────────────────────────────────

def _validate_picks(contexts: dict, picks: list, scheduler: str) -> None:
    """Hard guardrail: no scheduler may return an impossible observation —
    a slot the target isn't observable in, a dwell running past the night's
    end, or two dwells overlapping on one node.  Raises ValueError."""
    free = {nid: [True] * ctx.n_slots for nid, ctx in contexts.items()}
    for opp, slot in picks:
        ctx = contexts.get(opp.node_id)
        if ctx is None:
            raise ValueError(f"{scheduler}: unknown node {opp.node_id}")
        if slot not in opp.slots:
            raise ValueError(
                f"{scheduler}: {opp.name}@{opp.node_id} placed at slot "
                f"{slot} where the target is not observable")
        if slot < 0 or slot + opp.need > ctx.n_slots:
            raise ValueError(
                f"{scheduler}: {opp.name}@{opp.node_id} dwell "
                f"[{slot},{slot + opp.need}) exceeds the night "
                f"({ctx.n_slots} slots)")
        for s in range(slot, slot + opp.need):
            if not free[opp.node_id][s]:
                raise ValueError(
                    f"{scheduler}: overlapping dwells on {opp.node_id} "
                    f"at slot {s}")
            free[opp.node_id][s] = False


def _realized_cell_value(night_out: list, cells_by_target: dict,
                         ephemeris_by_target: dict, contexts: dict,
                         ch: dict) -> tuple:
    """Post-hoc science value of the accepted measurements, using the same
    cell/capture arithmetic for every scheduler: value = Σ ν·r·ρ_realized,
    with the residual ledger drawn down in time order."""
    R = {c.cell_id: max(0.0, min(1.0, c.residual))
         for cl in cells_by_target.values() for c in cl}
    total = 0.0
    accepted = [o for o in night_out if o.accepted]
    accepted.sort(key=lambda o: (o.slot_utc, o.node_id, o.target_id))
    for o in accepted:
        cl = cells_by_target.get(o.target_id) or []
        start = datetime.fromisoformat(o.slot_utc)
        end = start + timedelta(minutes=o.dwell_min)
        eph = ephemeris_by_target.get(o.target_id)
        for cell in cl:
            k = cellmod.kernel(cell, start, end, o.filter, eph, ch)
            if k < 1e-4:
                continue
            sigma = o.sigma_realized or o.sigma_pred
            rho = physics.capture(sigma, cell.sigma_ref) * k
            if o.mode == "time_series":
                rho *= max(o.clear_frac, 0.0)   # partial ride-through
            total += cell.nu * R[cell.cell_id] * rho
            R[cell.cell_id] *= (1.0 - rho)
    return total, R


def _update_target_state(world: World, night: int, night_out: list,
                         tstate: dict, ephemeris_by_target: dict,
                         alert_first_accept: dict) -> None:
    by_target: dict = {}
    for o in night_out:
        if o.accepted:
            by_target.setdefault(o.target_id, []).append(o)
    targets_by_id = {t.target_id: t for t in world.catalog}

    for tid, obs in by_target.items():
        target = targets_by_id.get(tid)
        st = tstate.get(tid)
        if target is None or st is None:
            continue
        latest = max(o.slot_utc for o in obs)
        st.last_accepted_utc = latest
        # EB phase-coverage residual draw-down (the T0 refresh).
        if target.ephemeris and st.phase_coverage:
            for o in obs:
                mid = (datetime.fromisoformat(o.slot_utc)
                       + timedelta(minutes=o.dwell_min / 2))
                ph = cellmod.phase_of(mid, target.ephemeris)
                if ph is None:
                    continue
                i = min(PHASE_BINS - 1, int(ph * PHASE_BINS))
                g = physics.capture(o.sigma_realized or o.sigma_pred, 0.03)
                st.phase_coverage[i] = max(0.0, st.phase_coverage[i] * (1 - g))
        # CV outburst detection: the belief flips only when the network SEES it.
        if target.cv:
            truth = world.cv_outburst[tid][night]
            if truth and not st.outburst_belief:
                st.outburst_belief = True
                st.outburst_belief_night = night
            elif not truth and st.outburst_belief:
                st.outburst_belief = False
        # Alert response bookkeeping.
        if target.alert_night is not None and tid not in alert_first_accept:
            alert_first_accept[tid] = min(o.slot_utc for o in obs)

    # Belief hygiene independent of tonight's data.
    for tid, st in tstate.items():
        target = targets_by_id[tid]
        if target.cv and st.outburst_belief \
                and night - st.outburst_belief_night > 14:
            st.outburst_belief = False
        if st.phase_coverage:
            # Slow residual recovery: ephemerides drift, seasons change.
            st.phase_coverage = [min(1.0, r + 0.02) for r in st.phase_coverage]


def _alert_summary(world: World, alert_first_accept: dict) -> list:
    out = []
    for t in world.catalog:
        if t.alert_night is None:
            continue
        arrival = datetime.fromisoformat(t.discovered_at)
        first = alert_first_accept.get(t.target_id)
        latency_h = None
        if first:
            latency_h = round(
                (datetime.fromisoformat(first) - arrival).total_seconds()
                / 3600.0, 2)
            latency_h = max(latency_h, 0.0)
        out.append({"target_id": t.target_id, "alert_night": t.alert_night,
                    "arrival": t.discovered_at, "first_accepted": first,
                    "latency_h": latency_h})
    return out


def _empty_night(night: int) -> dict:
    return {"night": night, "n_planned": 0, "n_executed": 0, "n_accepted": 0,
            "expected_deliveries": 0.0, "realized_value": 0.0,
            "schedulable_value": 0.0, "planned_minutes": 0.0,
            "wasted_minutes": 0.0, "distinct_targets": 0,
            "transit_events": 0, "transit_covered": 0,
            "transit_mean_coverage": 0.0, "utc_hours_covered": 0,
            "accepted_by_class": {}, "mean_clear_frac": 0.0}


def _night_metrics(night: int, contexts: dict, placements: list,
                   night_out: list, cells_by_target: dict,
                   realized_value: float, events: list, wx_by_node: dict,
                   mag_by_target: dict, world: World) -> dict:
    n_planned = len(placements)
    n_exec = sum(1 for o in night_out if o.executed)
    n_acc = sum(1 for o in night_out if o.accepted)
    planned_min = sum(o.dwell_min for o in night_out)
    wasted_min = sum(o.dwell_min for o in night_out if not o.accepted)
    schedulable = sum(c.nu * max(0.0, min(1.0, c.residual))
                      for cl in cells_by_target.values() for c in cl)
    by_class: dict = {}
    for o in night_out:
        if o.accepted:
            by_class[o.target_type] = by_class.get(o.target_type, 0) + 1

    # Transit coverage: union of accepted in-transit ride-through per event.
    covered, cov_fracs = 0, []
    for ev in events:
        half_h = max(ev["duration_hours"], 0.2) / 2.0
        in0 = ev["t_mid"] - timedelta(hours=half_h)
        in1 = ev["t_mid"] + timedelta(hours=half_h)
        frac = _event_coverage(night_out, ev["target_id"], in0, in1)
        cov_fracs.append(frac)
        if frac >= 0.5:
            covered += 1

    hours = set()
    for o in night_out:
        if o.accepted:
            hours.add(datetime.fromisoformat(o.slot_utc).hour)

    clears = [sum(1.0 for c in wx.truth_clear if c) / max(1, len(wx.truth_clear))
              for wx in wx_by_node.values()]
    return {
        "night": night,
        "n_planned": n_planned, "n_executed": n_exec, "n_accepted": n_acc,
        "expected_deliveries": round(sum(p.p for p in placements), 2),
        "realized_value": round(realized_value, 4),
        "schedulable_value": round(schedulable, 4),
        "planned_minutes": round(planned_min, 1),
        "wasted_minutes": round(wasted_min, 1),
        "distinct_targets": len({o.target_id for o in night_out}),
        "transit_events": len(events),
        "transit_covered": covered,
        "transit_mean_coverage": (round(sum(cov_fracs) / len(cov_fracs), 3)
                                  if cov_fracs else 0.0),
        "utc_hours_covered": len(hours),
        "accepted_by_class": by_class,
        "mean_clear_frac": (round(sum(clears) / len(clears), 3)
                            if clears else 0.0),
    }


def _event_coverage(night_out: list, target_id: str, in0: datetime,
                    in1: datetime) -> float:
    """Fraction of the in-transit window covered by ≥1 accepted time-series
    dwell, weighted by its clear ride-through fraction."""
    span_s = max((in1 - in0).total_seconds(), 1.0)
    marks = [0.0] * 20
    for o in night_out:
        if o.target_id != target_id or not o.accepted \
                or o.mode != "time_series":
            continue
        w0 = datetime.fromisoformat(o.slot_utc)
        w1 = w0 + timedelta(minutes=o.dwell_min)
        for i in range(20):
            seg0 = in0 + timedelta(seconds=span_s * i / 20)
            seg1 = in0 + timedelta(seconds=span_s * (i + 1) / 20)
            if w0 <= seg0 and seg1 <= w1:
                marks[i] = max(marks[i], o.clear_frac)
    return sum(marks) / len(marks)
