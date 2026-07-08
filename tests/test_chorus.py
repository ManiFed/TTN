#!/usr/bin/env python3
"""
Tests for CHORUS — the information-theoretic fleet coordinator (CHORUS.md).

These exercise the pure deterministic core (measurement physics, information
cells, scarcity, the submodular assignment engine) without a database or
astropy, by constructing contexts / opportunities / cells directly — the same
style as test_network_planner.

The assignment tests are the important ones: they verify that the coordination
behaviors the old optimizer needed knobs for (redundancy suppression, weather
hedging, same-site conservatism) *emerge* from the objective.
"""

import unittest
from datetime import datetime, timedelta, timezone

from cloud import tuning
from cloud.chorus import assign, cells, horizon, params as chorus_params, physics
from cloud.chorus.assign import ChorusOpportunity, SlotEval
from cloud.chorus.cells import InfoCell
from cloud.chorus.physics import ExposurePlan
from cloud.network_planner import NodeContext, STEP_MIN

BASE = datetime(2026, 6, 27, 22, 0, tzinfo=timezone.utc)
N_SLOTS = 16
END = BASE + timedelta(minutes=STEP_MIN * N_SLOTS)


def _node(**kw):
    """A nodes-table-shaped dict, Seestar-anchored."""
    n = {
        "node_id": kw.get("node_id", "N1"),
        "aperture_mm": 50.0, "focal_length_mm": 250.0,
        "pixel_scale_arcsec": 2.4, "fov_deg": 1.27,
        "max_exposure_s": 30.0, "cooled_camera": 0,
        "light_pollution_mpsas": 20.0, "elevation": 100.0,
        "mag_bright_limit": 6.0, "mag_faint_limit": 15.5,
        "mean_fwhm": 0.0, "min_altitude_deg": 25.0,
        "latitude": 40.0, "longitude": 0.0,
    }
    n.update(kw)
    return n


def _ctx(node_id, max_targets=1):
    return NodeContext(
        node={}, node_id=node_id, lat=40.0, lon=0.0,
        t0=BASE, t1=END, n_slots=N_SLOTS, utc_offset=timedelta(0),
        min_alt=25.0, horizon_mask=[], filters=["CV"], cooled=False,
        mount_type="alt_az", cloud_relax=0.0, max_targets=max_targets)


def _cell(target_id, nu=1.0, sigma_ref=0.05, cell_id=None, residual=1.0):
    return InfoCell(cell_id=cell_id or f"{target_id}:time:0",
                    target_id=target_id, kind="time", nu=nu,
                    sigma_ref=sigma_ref, residual=residual, t0=BASE, t1=END)


_SEQ = [0]


def _opp(node_id, target_id, *, p_sky=0.95, sigma=0.02, p_exec=0.9,
         p_accept=0.95, slots=(0, 2, 4, 6), variant="epoch", explore=0.0):
    _SEQ[0] += 1
    return ChorusOpportunity(
        node_id=node_id, target_id=target_id, name=target_id,
        ra_deg=10.0, dec_deg=40.0, mag=12.0, target_type="VAR",
        exposure=ExposurePlan(t_sub=10.0, n_sub=30, dwell_min=5.0, sigma=sigma),
        need=1,
        slots={s: SlotEval(p_sky=p_sky, sigma=sigma, alt=60.0, az=120.0)
               for s in slots},
        filter="CV", p_exec=p_exec, p_accept=p_accept, explore=explore,
        node_lon=0.0, variant=variant, duration_min=5.0, seq=_SEQ[0])


def _params(**overrides):
    p = dict(chorus_params.DEFAULTS)
    p.update(overrides)
    return p


def _run(contexts, opps, cell_map, params=None, budget=0.0, seed=7):
    return assign.assign(contexts, opps, cell_map, params or _params(),
                         seed=seed, local_search_ms=budget)


# ── Physics: hardware differentiation is continuous, not a tier label ────────

class PhysicsTest(unittest.TestCase):
    def test_aperture_cuts_faint_end_error(self):
        small = physics.sigma_total(_node(), 15.5, 60.0, 30.0, 24)
        big = physics.sigma_total(_node(aperture_mm=200.0), 15.5, 60.0, 30.0, 24)
        self.assertLess(big, small * 0.5)

    def test_cooling_helps_faint_targets(self):
        warm = physics.sigma_total(_node(), 15.0, 60.0, 30.0, 24)
        cold = physics.sigma_total(_node(cooled_camera=1), 15.0, 60.0, 30.0, 24)
        self.assertLess(cold, warm)

    def test_wide_field_lowers_ensemble_floor_on_bright_targets(self):
        # Bright star: instrumental error is tiny, the comp-star ensemble
        # dominates — so FoV (comparison star count) decides the error.
        narrow = physics.sigma_total(_node(fov_deg=0.3), 9.0, 60.0, 30.0, 24)
        wide = physics.sigma_total(_node(fov_deg=1.27), 9.0, 60.0, 30.0, 24)
        self.assertLess(wide, narrow)

    def test_airmass_degrades(self):
        high = physics.sigma_total(_node(), 14.0, 30.0, 30.0, 24)
        low = physics.sigma_total(_node(), 14.0, 80.0, 30.0, 24)
        self.assertLess(low, high)

    def test_kappa_inflates(self):
        base = physics.sigma_total(_node(), 13.0, 60.0, 30.0, 24, kappa=1.0)
        infl = physics.sigma_total(_node(), 13.0, 60.0, 30.0, 24, kappa=4.0)
        self.assertAlmostEqual(infl, base * 2.0, places=6)

    def test_capture_saturation(self):
        self.assertAlmostEqual(physics.capture(0.05, 0.05), 0.5, places=6)
        self.assertGreater(physics.capture(0.01, 0.05), 0.9)
        self.assertLess(physics.capture(0.25, 0.05), 0.05)

    def test_exposure_respects_sub_cap_and_scales_with_faintness(self):
        node = _node(max_exposure_s=30.0)
        bright = physics.best_exposure(node, 9.0, 60.0, 0.05)
        faint = physics.best_exposure(node, 15.0, 60.0, 0.05)
        self.assertLessEqual(bright.t_sub, 30.0)
        self.assertLessEqual(faint.t_sub, 30.0)
        self.assertGreaterEqual(faint.dwell_min, bright.dwell_min)


# ── Cells: class strategy templates ──────────────────────────────────────────

class CellTemplateTest(unittest.TestCase):
    def _target(self, ttype, **kw):
        t = {"target_id": "T1", "name": "T1", "target_type": ttype,
             "priority": 0.8, "cadence_hours": 4.0, "mag": 12.0,
             "ra_deg": 10.0, "dec_deg": 40.0}
        t.update(kw)
        return t

    def test_eb_with_ephemeris_gets_weighted_phase_bins(self):
        state = {"ephemeris": {"period_days": 2.7, "epoch_jd": 2460000.0}}
        cl = cells.compile_cells(self._target("EB"), state, BASE, END,
                                 _params(), scarcity=1.0)
        self.assertEqual(len(cl), cells.PHASE_BINS)
        self.assertTrue(all(c.kind == "phase" for c in cl))
        eclipse = max(cl, key=lambda c: c.nu)
        quad = [c for c in cl if "quadrature" in c.label]
        self.assertIn("eclipse", eclipse.label)
        self.assertTrue(quad and quad[0].nu < eclipse.nu)

    def test_cv_hazard_grows_with_neglect(self):
        fresh = {"last_accepted_utc": (BASE - timedelta(hours=2)).isoformat()}
        stale = {"last_accepted_utc": (BASE - timedelta(days=14)).isoformat()}
        nu_fresh = cells.compile_cells(self._target("CV"), fresh, BASE, END,
                                       _params(), 1.0)[0].nu
        nu_stale = cells.compile_cells(self._target("CV"), stale, BASE, END,
                                       _params(), 1.0)[0].nu
        self.assertLess(nu_fresh, nu_stale)

    def test_cv_outburst_escalates_to_dense_campaign(self):
        quiet = cells.compile_cells(self._target("CV"), {}, BASE, END,
                                    _params(), 1.0)
        burst = cells.compile_cells(self._target("CV"), {"outburst": True},
                                    BASE, END, _params(), 1.0)
        self.assertEqual(len(quiet), 1)
        self.assertGreater(len(burst), 2)
        self.assertLess(burst[0].sigma_ref, quiet[0].sigma_ref)

    def test_transit_cells_cover_event_and_weight_contacts(self):
        t_mid = BASE + timedelta(hours=2)
        cl = cells.transit_cells("TX", "WASP-X b", t_mid, 2.0, 12.0,
                                 BASE, BASE + timedelta(hours=4),
                                 _params(), 1.0)
        self.assertGreaterEqual(len(cl), 4)
        by_label = {c.label.split()[-1]: c for c in cl}
        self.assertGreater(by_label["ingress"].nu, by_label["pre"].nu)

    def test_band_cells_only_when_fleet_has_the_filter(self):
        t = self._target("SN", discovered_at=BASE.isoformat())
        no_bands = cells.compile_cells(t, {}, BASE, END, _params(), 1.0,
                                       band_union={"CV"})
        with_v = cells.compile_cells(t, {}, BASE, END, _params(), 1.0,
                                     band_union={"CV", "V"})
        self.assertFalse([c for c in no_bands if c.kind == "band"])
        self.assertTrue([c for c in with_v if c.kind == "band"])

    def test_scarcity_scales_value(self):
        t = self._target("VAR")
        abundant = cells.compile_cells(t, {}, BASE, END, _params(), 0.2)
        rare = cells.compile_cells(t, {}, BASE, END, _params(), 1.0)
        self.assertLess(abundant[0].nu, rare[0].nu)

    def test_kernel_point_semantics_and_event_coverage(self):
        c = _cell("T1")
        inside = cells.kernel(c, BASE + timedelta(hours=1),
                              BASE + timedelta(hours=1, minutes=5),
                              "CV", None, _params())
        outside = cells.kernel(c, END + timedelta(hours=12),
                               END + timedelta(hours=12, minutes=5),
                               "CV", None, _params())
        self.assertAlmostEqual(inside, 1.0)
        self.assertLess(outside, 0.01)
        ev = InfoCell(cell_id="e", target_id="T1", kind="event", nu=1.0,
                      sigma_ref=0.01, t0=BASE, t1=BASE + timedelta(hours=2))
        half = cells.kernel(ev, BASE, BASE + timedelta(hours=1),
                            "CV", None, _params())
        self.assertAlmostEqual(half, 0.5, places=3)

    def test_cell_roundtrip(self):
        c = _cell("T1", nu=0.7, sigma_ref=0.03)
        c2 = InfoCell.from_dict(c.to_dict())
        self.assertEqual(c2.cell_id, c.cell_id)
        self.assertAlmostEqual(c2.nu, c.nu, places=4)
        self.assertEqual(c2.t0, c.t0)


# ── Horizon: scarcity prices tonight against the future ──────────────────────

class HorizonTest(unittest.TestCase):
    def test_unreachable_target_is_maximally_scarce(self):
        nodes = [_node()]
        p_exec = {"N1": 0.9}
        def clear(node, when):
            return 0.6

        reachable = {"target_id": "A", "ra_deg": (horizon.sun_ra_deg(BASE) + 180) % 360,
                     "dec_deg": 40.0}
        unreachable = {"target_id": "B", "ra_deg": 10.0, "dec_deg": -40.0}
        s_reach = horizon.scarcity(reachable, nodes, p_exec, clear,
                                   _params(), today=BASE)
        s_unreach = horizon.scarcity(unreachable, nodes, p_exec, clear,
                                     _params(), today=BASE)
        self.assertLess(s_reach, 0.5)
        self.assertEqual(s_unreach, 1.0)

    def test_more_reliable_fleet_lowers_scarcity(self):
        nodes = [_node()]
        t = {"target_id": "A", "ra_deg": (horizon.sun_ra_deg(BASE) + 180) % 360,
             "dec_deg": 40.0}
        def clear(node, when):
            return 0.6

        s_good = horizon.scarcity(t, nodes, {"N1": 0.95}, clear, _params(), today=BASE)
        s_poor = horizon.scarcity(t, nodes, {"N1": 0.30}, clear, _params(), today=BASE)
        self.assertLess(s_good, s_poor)


# ── Assignment: emergent coordination ─────────────────────────────────────────

class EmergentCoordinationTest(unittest.TestCase):
    def test_redundancy_suppression_under_good_weather(self):
        """Two reliable nodes under clear skies, one shared high-value target
        plus a weaker unique target each: the shared target is covered ONCE and
        the freed capacity goes to a unique target — no redundancy_decay knob."""
        contexts = {"A": _ctx("A"), "B": _ctx("B")}
        cell_map = {"shared": [_cell("shared", nu=1.0)],
                    "uniq_a": [_cell("uniq_a", nu=0.4)],
                    "uniq_b": [_cell("uniq_b", nu=0.4)]}
        opps = {"A": [_opp("A", "shared"), _opp("A", "uniq_a")],
                "B": [_opp("B", "shared"), _opp("B", "uniq_b")]}
        placements, _, _ = _run(contexts, opps, cell_map)
        placed = [p.opp.target_id for p in placements]
        self.assertEqual(placed.count("shared"), 1, placed)
        self.assertTrue(set(placed) & {"uniq_a", "uniq_b"}, placed)

    def test_weather_hedging_emerges_under_cloud_risk(self):
        """Identical value landscape, but p_sky drops to 0.35: the expected
        residual left by one risky booking makes the second node's copy worth
        more than its unique alternative — cross-site hedging from arithmetic."""
        contexts = {"A": _ctx("A"), "B": _ctx("B")}
        cell_map = {"shared": [_cell("shared", nu=1.0)],
                    "uniq_a": [_cell("uniq_a", nu=0.4)],
                    "uniq_b": [_cell("uniq_b", nu=0.4)]}
        opps = {"A": [_opp("A", "shared", p_sky=0.35),
                      _opp("A", "uniq_a", p_sky=0.35)],
                "B": [_opp("B", "shared", p_sky=0.35),
                      _opp("B", "uniq_b", p_sky=0.35)]}
        placements, _, _ = _run(contexts, opps, cell_map)
        placed = [p.opp.target_id for p in placements]
        self.assertEqual(placed.count("shared"), 2, placed)

    def test_same_site_repeat_cannot_pose_as_weather_hedge(self):
        """A second booking of the same target at the SAME node shares its sky,
        so the correlation cap suppresses it — while the identical booking from
        a second site goes through."""
        params = _params(min_marginal=0.1)
        one_site = {"A": _ctx("A", max_targets=2)}
        cell_map = {"shared": [_cell("shared", nu=1.0)]}
        opps_one = {"A": [_opp("A", "shared", p_sky=0.35, slots=(0, 2)),
                          _opp("A", "shared", p_sky=0.35, slots=(8, 10),
                               variant="epoch_b")]}
        placements, _, _ = _run(one_site, opps_one, cell_map, params)
        self.assertEqual(len(placements), 1)

        two_sites = {"A": _ctx("A"), "B": _ctx("B")}
        cell_map2 = {"shared": [_cell("shared", nu=1.0)]}
        opps_two = {"A": [_opp("A", "shared", p_sky=0.35)],
                    "B": [_opp("B", "shared", p_sky=0.35)]}
        placements2, _, _ = _run(two_sites, opps_two, cell_map2, params)
        self.assertEqual(len(placements2), 2)

    def test_hardware_routes_faint_work_to_the_capable_scope(self):
        """One faint-demand cell (σ_ref 0.02): the node whose physics delivers
        σ=0.01 wins it over the node stuck at σ=0.2, regardless of order."""
        contexts = {"seestar": _ctx("seestar"), "big": _ctx("big")}
        cell_map = {"faint": [_cell("faint", nu=1.0, sigma_ref=0.02)]}
        opps = {"seestar": [_opp("seestar", "faint", sigma=0.20)],
                "big": [_opp("big", "faint", sigma=0.01)]}
        placements, _, _ = _run(contexts, opps, cell_map,
                                _params(max_obs_per_target=1))
        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].node_id, "big")

    def test_exploration_bonus_lifts_uncertain_nodes(self):
        """With everything else equal, the node with a wide reliability
        posterior gets the assignment — scheduling as experiment design."""
        contexts = {"vet": _ctx("vet"), "new": _ctx("new")}
        cell_map = {"t": [_cell("t", nu=0.5)]}
        opps = {"vet": [_opp("vet", "t", explore=0.0)],
                "new": [_opp("new", "t", explore=0.8)]}
        placements, _, _ = _run(contexts, opps, cell_map,
                                _params(max_obs_per_target=1))
        self.assertEqual(placements[0].node_id, "new")

    def test_deterministic(self):
        def build():
            _SEQ[0] = 0
            contexts = {"A": _ctx("A", 2), "B": _ctx("B", 2)}
            cell_map = {f"t{i}": [_cell(f"t{i}", nu=0.3 + 0.1 * i)]
                        for i in range(5)}
            opps = {"A": [_opp("A", f"t{i}") for i in range(5)],
                    "B": [_opp("B", f"t{i}", p_sky=0.5) for i in range(5)]}
            return contexts, opps, cell_map
        sig = []
        for _ in range(2):
            contexts, opps, cell_map = build()
            placements, _, _ = _run(contexts, opps, cell_map, budget=100)
            sig.append(sorted((p.node_id, p.opp.target_id, p.slot)
                              for p in placements))
        self.assertEqual(sig[0], sig[1])

    def test_local_search_never_regresses(self):
        contexts = {"A": _ctx("A", 3), "B": _ctx("B", 3)}
        cell_map = {f"t{i}": [_cell(f"t{i}", nu=0.3 + 0.05 * i)]
                    for i in range(6)}
        opps = {"A": [_opp("A", f"t{i}") for i in range(6)],
                "B": [_opp("B", f"t{i}") for i in range(6)]}
        _, _, stats = _run(contexts, opps, cell_map, budget=100)
        self.assertGreaterEqual(stats["final_phi"], stats["greedy_phi"])

    def test_capacity_and_portfolio_caps_respected(self):
        contexts = {"A": _ctx("A", max_targets=2)}
        cell_map = {f"t{i}": [_cell(f"t{i}", nu=1.0)] for i in range(5)}
        opps = {"A": [_opp("A", f"t{i}") for i in range(5)]}
        placements, _, _ = _run(contexts, opps, cell_map)
        self.assertLessEqual(len(placements), 2)


# ── Tuning integration: the chorus group ──────────────────────────────────────

class ChorusTuningTest(unittest.TestCase):
    def test_chorus_group_registered(self):
        self.assertIn("chorus", tuning.SCALAR_GROUPS)
        self.assertEqual(set(tuning.CHORUS_KEYS), set(chorus_params.KEYS))

    def test_chorus_clamp_respects_bounds_and_delta(self):
        cur = dict(tuning.DEFAULT_CHORUS_PARAMS)
        proposed = {k: 9999.0 for k in tuning.CHORUS_KEYS}
        new = tuning._clamp_scalars(
            cur, proposed, 0.15, keys=tuning.CHORUS_KEYS,
            defaults=tuning.DEFAULT_CHORUS_PARAMS, bounds=tuning.CHORUS_BOUNDS)
        for k in tuning.CHORUS_KEYS:
            lo, hi = tuning.CHORUS_BOUNDS[k]
            self.assertGreaterEqual(new[k], lo)
            self.assertLessEqual(new[k], hi)
            cap = max(0.15 * abs(cur[k]), 0.15 * (hi - lo) * 0.1)
            # + 5e-5: _clamp_scalars rounds the applied value to 4 dp
            self.assertLessEqual(new[k] - cur[k], cap + 5e-5 + 1e-6)

    def test_default_params_include_chorus_seed(self):
        params = tuning._default_params(
            {"scheduler": {"chorus_params": {"scarcity_gamma": 0.9}}})
        self.assertAlmostEqual(params["chorus"]["scarcity_gamma"], 0.9)
        self.assertIn("exploration_beta", params["chorus"])

    def test_gate_passes_through_when_archive_unavailable(self):
        # No DB in tests → backtest.gate reports "archive unavailable" and the
        # trust region alone governs (params unchanged by the gate).
        current = tuning._canonical({})
        new = tuning._canonical(
            {"chorus": {**tuning.DEFAULT_CHORUS_PARAMS,
                        "exploration_beta": 0.25}})
        gated, note = tuning._gate_chorus_group(current, new, {})
        self.assertAlmostEqual(gated["chorus"]["exploration_beta"], 0.25)
        self.assertEqual(note, "")


# ── Ledger priors (pure path) ─────────────────────────────────────────────────

class LedgerPriorTest(unittest.TestCase):
    def test_new_node_gets_principled_priors(self):
        from cloud.chorus import ledger
        vec = ledger.node_vector("never_seen")
        self.assertAlmostEqual(vec["p_exec"], 4.0 / 6.0, places=3)
        self.assertAlmostEqual(vec["p_accept"], 4.0 / 6.0, places=3)
        self.assertEqual(vec["kappa"], 1.0)
        self.assertGreater(vec["explore"], 0.3)


# ── Backtest serialization roundtrip ──────────────────────────────────────────

class BacktestSerializationTest(unittest.TestCase):
    def test_opportunity_roundtrip(self):
        from cloud.chorus import backtest
        o = _opp("A", "t1", p_sky=0.42, sigma=0.031)
        o2 = backtest._deser_opp(backtest._ser_opp(o))
        self.assertEqual(o2.key, o.key)
        self.assertAlmostEqual(o2.slots[0].p_sky, 0.42, places=3)
        self.assertAlmostEqual(o2.exposure.sigma, o.exposure.sigma, places=4)

    def test_context_roundtrip(self):
        from cloud.chorus import backtest
        ctx = _ctx("A", max_targets=7)
        ctx2 = backtest._deser_ctx("A", backtest._ser_ctx(ctx))
        self.assertEqual(ctx2.n_slots, ctx.n_slots)
        self.assertEqual(ctx2.t0, ctx.t0)
        self.assertEqual(ctx2.max_targets, 7)


if __name__ == "__main__":
    unittest.main()
