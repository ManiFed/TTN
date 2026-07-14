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


def _lon_for(node_id: str) -> float:
    """Deterministic, well-separated synthetic longitude per node_id (not a
    real site) — distinct test nodes must sit far enough apart that
    assign.weather_corr_factor treats them as weather-independent by
    default, the way genuinely different sites would in production."""
    return float((sum(ord(c) for c in node_id) * 37) % 360)


def _ctx(node_id, max_targets=1):
    return NodeContext(
        node={}, node_id=node_id, lat=40.0, lon=_lon_for(node_id),
        t0=BASE, t1=END, n_slots=N_SLOTS, utc_offset=timedelta(0),
        min_alt=25.0, horizon_mask=[], filters=["CV"], cooled=False,
        mount_type="alt_az", cloud_relax=0.0, max_targets=max_targets)


def _cell(target_id, nu=1.0, sigma_ref=0.05, cell_id=None, residual=1.0):
    return InfoCell(cell_id=cell_id or f"{target_id}:time:0",
                    target_id=target_id, kind="time", nu=nu,
                    sigma_ref=sigma_ref, residual=residual, t0=BASE, t1=END)


_SEQ = [0]


def _opp(node_id, target_id, *, p_sky=0.95, sigma=0.02, p_exec=0.9,
         p_accept=0.95, slots=(0, 2, 4, 6), variant="epoch", explore=0.0,
         filter="CV"):
    _SEQ[0] += 1
    return ChorusOpportunity(
        node_id=node_id, target_id=target_id, name=target_id,
        ra_deg=10.0, dec_deg=40.0, mag=12.0, target_type="VAR",
        exposure=ExposurePlan(t_sub=10.0, n_sub=30, dwell_min=5.0, sigma=sigma),
        need=1,
        slots={s: SlotEval(p_sky=p_sky, sigma=sigma, alt=60.0, az=120.0)
               for s in slots},
        filter=filter, p_exec=p_exec, p_accept=p_accept, explore=explore,
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

    def test_default_family_also_gets_band_cells_for_cross_node_color(self):
        # Ordinary field variables (family "default", e.g. target_type "VAR")
        # get the same cross-node multi-band opportunity as transients —
        # previously band cells only existed for the transient family.
        t = self._target("VAR")
        no_bands = cells.compile_cells(t, {}, BASE, END, _params(), 1.0,
                                       band_union={"CV"})
        with_bands = cells.compile_cells(t, {}, BASE, END, _params(), 1.0,
                                         band_union={"CV", "B", "V"})
        self.assertFalse([c for c in no_bands if c.kind == "band"])
        band_cells = [c for c in with_bands if c.kind == "band"]
        self.assertEqual({c.band for c in band_cells}, {"B", "V"})
        # Cadence cells are untouched — bands are purely additive.
        self.assertEqual(len([c for c in with_bands if c.kind != "band"]),
                         len([c for c in no_bands if c.kind != "band"]))

    def test_scarcity_scales_value(self):
        t = self._target("VAR")
        abundant = cells.compile_cells(t, {}, BASE, END, _params(), 0.2)
        rare = cells.compile_cells(t, {}, BASE, END, _params(), 1.0)
        self.assertLess(abundant[0].nu, rare[0].nu)

    def test_ring2_template_overrides_default_band_multiplier(self):
        t = self._target("VAR")
        default = cells.compile_cells(t, {}, BASE, END, _params(), 1.0,
                                      band_union={"B"})
        overridden = cells.compile_cells(
            t, {}, BASE, END, _params(), 1.0, band_union={"B"},
            templates={"default": {"band_value_mult": 0.9}})
        band_default = next(c for c in default if c.kind == "band")
        band_overridden = next(c for c in overridden if c.kind == "band")
        self.assertGreater(band_overridden.nu, band_default.nu)

    def test_ring2_template_overrides_transient_age_segments(self):
        t = self._target("SN", discovered_at=BASE.isoformat())
        # discovered "now" -> the recent segment applies by default (age 0
        # days < 5). Override seg_recent_days to 0 so it falls straight to
        # the mid segment instead, and seg_mid_mult so the effect is visible.
        default = cells.compile_cells(t, {}, BASE, END, _params(), 1.0)
        overridden = cells.compile_cells(
            t, {}, BASE, END, _params(), 1.0,
            templates={"transient": {"seg_recent_days": 0.0,
                                     "seg_mid_mult": 3.0}})
        self.assertLess(default[0].nu, overridden[0].nu)

    def test_ring2_template_overrides_lpv_min_width(self):
        t = self._target("LPV", cadence_hours=1.0)
        default = cells.compile_cells(t, {}, BASE, END, _params(), 1.0)
        overridden = cells.compile_cells(
            t, {}, BASE, END, _params(), 1.0,
            templates={"lpv": {"min_width_h": 999.0}})
        # cadence_hours=1.0 is far below both floors, so width_h is exactly
        # the floor in each case — a much larger override floor should
        # produce a materially wider (fewer, broader) set of cells.
        self.assertLessEqual(len(overridden), len(default))

    def test_ring2_no_template_matches_todays_hardcoded_defaults(self):
        # templates=None (or missing the family) must reproduce exactly
        # today's literals — Ring 2 is purely additive until a template is
        # actually promoted to 'live'.
        t = self._target("VAR")
        no_arg = cells.compile_cells(t, {}, BASE, END, _params(), 1.0,
                                     band_union={"B"})
        empty_templates = cells.compile_cells(t, {}, BASE, END, _params(), 1.0,
                                              band_union={"B"}, templates={})
        self.assertEqual([c.nu for c in no_arg], [c.nu for c in empty_templates])

    def test_scarcity_urgency_power_default_is_linear(self):
        # scarcity_urgency_power defaults to 1.0 — a no-op, identical to the
        # pre-existing linear scarcity scaling.
        p = _params()
        self.assertEqual(p["scarcity_urgency_power"], 1.0)
        self.assertAlmostEqual(cells._urgency(0.7, p), 0.7)

    def test_scarcity_urgency_power_sharpens_last_chance_targets(self):
        # A last-chance target (S near 1) and a merely-scarce one (S=0.7) are
        # 0.3 apart under the raw linear multiplier. Raising the urgency
        # power should widen that separation (S=1 is a fixed point of x**p,
        # so the near-1 target barely moves while the 0.7 one is suppressed
        # further), converting "somewhat scarce" into a comparatively much
        # smaller share of value than "truly last chance."
        t = self._target("VAR")
        linear = _params(scarcity_urgency_power=1.0)
        sharpened = _params(scarcity_urgency_power=3.0)

        last_chance_linear = cells.compile_cells(t, {}, BASE, END, linear, 0.98)[0].nu
        scarce_linear = cells.compile_cells(t, {}, BASE, END, linear, 0.7)[0].nu
        last_chance_sharp = cells.compile_cells(t, {}, BASE, END, sharpened, 0.98)[0].nu
        scarce_sharp = cells.compile_cells(t, {}, BASE, END, sharpened, 0.7)[0].nu

        ratio_linear = last_chance_linear / scarce_linear
        ratio_sharp = last_chance_sharp / scarce_sharp
        self.assertGreater(ratio_sharp, ratio_linear)

    def test_urgency_clamps_and_floors_power_at_one(self):
        # scarcity is clamped to [0,1] and power floors at 1.0 (no inversion).
        self.assertAlmostEqual(cells._urgency(1.4, _params()), 1.0)
        self.assertAlmostEqual(cells._urgency(-0.5, _params()), 0.0)
        self.assertAlmostEqual(
            cells._urgency(0.5, _params(scarcity_urgency_power=0.2)), 0.5)

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
        the freed capacity goes to a unique target — no redundancy_decay knob.
        (Core tier only: the night filler may later pack the redundant repeat
        into otherwise-idle slots, which is the point of the filler.)"""
        contexts = {"A": _ctx("A"), "B": _ctx("B")}
        cell_map = {"shared": [_cell("shared", nu=1.0)],
                    "uniq_a": [_cell("uniq_a", nu=0.4)],
                    "uniq_b": [_cell("uniq_b", nu=0.4)]}
        opps = {"A": [_opp("A", "shared"), _opp("A", "uniq_a")],
                "B": [_opp("B", "shared"), _opp("B", "uniq_b")]}
        placements, _, _ = _run(contexts, opps, cell_map)
        placed = [p.opp.target_id for p in placements if p.tier == "core"]
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
        self.assertEqual(
            len([p for p in placements if p.tier == "core"]), 1)

        two_sites = {"A": _ctx("A"), "B": _ctx("B")}
        cell_map2 = {"shared": [_cell("shared", nu=1.0)]}
        opps_two = {"A": [_opp("A", "shared", p_sky=0.35)],
                    "B": [_opp("B", "shared", p_sky=0.35)]}
        placements2, _, _ = _run(two_sites, opps_two, cell_map2, params)
        self.assertEqual(len(placements2), 2)

    def test_weather_corr_factor_exact_same_node_uses_same_site_factor(self):
        p = _params(same_site_repeat_factor=0.3)
        contexts = {"A": _ctx("A")}
        self.assertAlmostEqual(
            assign.weather_corr_factor("A", "A", contexts, p), 0.3)

    def test_weather_corr_factor_far_apart_nodes_are_independent(self):
        p = _params(weather_corr_radius_km=50.0)
        contexts = {"A": _ctx("A"), "B": _ctx("B")}
        self.assertAlmostEqual(
            assign.weather_corr_factor("A", "B", contexts, p), 1.0, places=2)

    def test_weather_corr_factor_close_nodes_interpolate_toward_same_site(self):
        # Two sites 1 km apart (well inside a 50 km correlation radius) should
        # land close to the same-site factor, not full independence.
        p = _params(same_site_repeat_factor=0.25, weather_corr_radius_km=50.0)
        contexts = {"A": _ctx("A"), "B": _ctx("B")}
        # ~1 km of latitude ≈ 0.009 deg.
        contexts["B"].lat = contexts["A"].lat
        contexts["B"].lon = contexts["A"].lon + 0.009
        factor = assign.weather_corr_factor("A", "B", contexts, p)
        self.assertLess(factor, 0.4)
        self.assertGreaterEqual(factor, 0.25)

    def test_nearby_node_repeat_suppressed_like_same_site(self):
        # Two DIFFERENT nodes, but close enough (per weather_corr_radius_km)
        # to be treated as sharing a sky: a second booking of the same
        # target on the nearby node should be suppressed the same way a
        # same-node repeat is, not treated as an independent weather hedge.
        params = _params(min_marginal=0.1, weather_corr_radius_km=50.0)
        contexts = {"A": _ctx("A"), "B": _ctx("B")}
        contexts["B"].lat = contexts["A"].lat
        contexts["B"].lon = contexts["A"].lon + 0.009   # ~1 km away
        cell_map = {"shared": [_cell("shared", nu=1.0)]}
        opps = {"A": [_opp("A", "shared", p_sky=0.35)],
               "B": [_opp("B", "shared", p_sky=0.35)]}
        placements, _, _ = _run(contexts, opps, cell_map, params)
        self.assertEqual(
            len([p for p in placements if p.tier == "core"]), 1)

    def test_cross_node_multi_band_pairing_emerges(self):
        """Two nodes with complementary filters (A only has 'B', B only has
        'V') on the same bright generic-family target: each captures the
        band cell only it can, so the fleet ends up with one epoch's
        color/SED data instead of two identical single-band measurements —
        no special-cased pairing logic, just each node chasing the cell
        its own filter can touch (cells.kernel's band-match gate)."""
        t = {"target_id": "T1", "name": "T1", "target_type": "VAR",
            "priority": 0.8, "cadence_hours": 4.0, "mag": 12.0,
            "ra_deg": 10.0, "dec_deg": 40.0}
        cell_list = cells.compile_cells(t, {}, BASE, END, _params(), 1.0,
                                        band_union={"B", "V"})
        self.assertTrue(any(c.kind == "band" for c in cell_list))
        contexts = {"A": _ctx("A"), "B": _ctx("B")}
        opps = {"A": [_opp("A", "T1", filter="B", variant="band_b")],
               "B": [_opp("B", "T1", filter="V", variant="band_v")]}
        placements, _, _ = _run(contexts, opps, {"T1": cell_list},
                                _params(max_obs_per_target=2))
        placed_filters = {p.node_id: p.opp.filter for p in placements}
        self.assertEqual(placed_filters, {"A": "B", "B": "V"})

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
        # The core value greedy honors max_targets; anything beyond the cap
        # can only be night-filler tier (bounded by occupancy and
        # filler_max_targets_per_night instead).
        contexts = {"A": _ctx("A", max_targets=2)}
        cell_map = {f"t{i}": [_cell(f"t{i}", nu=1.0)] for i in range(5)}
        opps = {"A": [_opp("A", f"t{i}") for i in range(5)]}
        placements, _, stats = _run(contexts, opps, cell_map)
        core = [p for p in placements if p.tier == "core"]
        self.assertLessEqual(len(core), 2)
        self.assertEqual(len(placements) - len(core), stats["n_filler"])

    def test_capacity_cap_hard_with_filler_disabled(self):
        contexts = {"A": _ctx("A", max_targets=2)}
        cell_map = {f"t{i}": [_cell(f"t{i}", nu=1.0)] for i in range(5)}
        opps = {"A": [_opp("A", f"t{i}") for i in range(5)]}
        placements, _, stats = _run(contexts, opps, cell_map,
                                    params=_params(filler_min_marginal=0.0))
        self.assertLessEqual(len(placements), 2)
        self.assertEqual(stats["n_filler"], 0)


# ── Night filler: dark time is never left unassigned ──────────────────────────

class NightFillerTest(unittest.TestCase):
    def test_filler_packs_otherwise_free_slots(self):
        # One node capped at a single core target but with four free windows:
        # the fill pass must occupy them with the remaining targets.
        contexts = {"A": _ctx("A", max_targets=1)}
        cell_map = {f"t{i}": [_cell(f"t{i}", nu=1.0)] for i in range(4)}
        opps = {"A": [_opp("A", f"t{i}") for i in range(4)]}
        placements, _, stats = _run(contexts, opps, cell_map)
        self.assertEqual(len(placements), 4)
        self.assertEqual(stats["n_filler"], 3)
        slots = sorted(p.slot for p in placements)
        self.assertEqual(len(set(slots)), 4)   # distinct occupancy, no overlap

    def test_sub_min_marginal_places_only_as_filler(self):
        # Value below the core greedy's floor but above the filler floor:
        # nothing lands as core, everything that fits lands as filler.
        contexts = {"A": _ctx("A", max_targets=2)}
        cell_map = {"t0": [_cell("t0", nu=0.01)]}
        opps = {"A": [_opp("A", "t0")]}
        placements, _, stats = _run(contexts, opps, cell_map)
        self.assertTrue(placements)
        self.assertTrue(all(p.tier == "filler" for p in placements))
        self.assertEqual(stats["n_filler"], len(placements))

    def test_filler_bounded_by_safety_valve(self):
        contexts = {"A": _ctx("A", max_targets=1)}
        cell_map = {f"t{i}": [_cell(f"t{i}", nu=1.0)] for i in range(5)}
        opps = {"A": [_opp("A", f"t{i}") for i in range(5)]}
        placements, _, stats = _run(
            contexts, opps, cell_map,
            params=_params(filler_max_targets_per_night=2.0))
        self.assertEqual(stats["n_filler"], 2)
        self.assertLessEqual(len(placements), 3)   # 1 core + 2 filler

    def test_filler_respects_max_obs_per_target(self):
        # Two variants of the same low-value target: the filler must not stack
        # epochs past the per-target portfolio cap.
        contexts = {"A": _ctx("A", max_targets=2)}
        cell_map = {"t0": [_cell("t0", nu=0.01)]}
        opps = {"A": [_opp("A", "t0", slots=(0, 2)),
                      _opp("A", "t0", slots=(8, 10), variant="epoch_b")]}
        placements, _, _ = _run(contexts, opps, cell_map,
                                params=_params(max_obs_per_target=1.0))
        self.assertLessEqual(len(placements), 1)

    def test_filler_is_deterministic(self):
        contexts = {"A": _ctx("A", max_targets=1)}
        cell_map = {f"t{i}": [_cell(f"t{i}", nu=1.0)] for i in range(4)}
        runs = []
        for _ in range(2):
            opps = {"A": [_opp("A", f"t{i}") for i in range(4)]}
            placements, _, _ = _run(contexts, opps, cell_map)
            runs.append(sorted((p.opp.target_id, p.slot, p.tier)
                               for p in placements))
        self.assertEqual(runs[0], runs[1])


class ContingencyLadderTest(unittest.TestCase):
    def test_ladder_size_and_runnable_fields(self):
        from cloud.chorus import assign as assign_mod
        from cloud.chorus import perform
        contexts = {"A": _ctx("A", max_targets=1)}
        cell_map = {f"t{i}": [_cell(f"t{i}", nu=1.0)] for i in range(5)}
        opps = {"A": [_opp("A", f"t{i}") for i in range(5)]}
        params = _params(filler_min_marginal=0.0)   # leave opps uncommitted
        placements, _, _ = _run(contexts, opps, cell_map, params=params)
        final_state = assign_mod.replay(contexts, cell_map, placements, params)

        ladder = perform.contingency_ladder(
            contexts["A"], opps["A"], final_state, cell_map, params, top_k=3)
        alts = ladder.get("alternates", [])
        self.assertEqual(len(alts), 3)
        for alt in alts:
            # Every alternate is directly runnable as a node schedule item.
            for key in ("target", "ra", "dec", "expDur", "expCount", "filter",
                        "startTime", "score", "observation_mode",
                        "duration_minutes", "expected_info"):
                self.assertIn(key, alt)
            self.assertGreater(alt["expected_info"], 0.0)

    def test_ladder_ranked_by_value(self):
        from cloud.chorus import assign as assign_mod
        from cloud.chorus import perform
        contexts = {"A": _ctx("A", max_targets=1)}
        cell_map = {"big": [_cell("big", nu=1.0)],
                    "small": [_cell("small", nu=0.3)]}
        opps = {"A": [_opp("A", "big"), _opp("A", "small")]}
        # Nothing committed at all: an empty replay leaves both as alternates.
        final_state = assign_mod.replay(contexts, cell_map, [], _params())
        ladder = perform.contingency_ladder(
            contexts["A"], opps["A"], final_state, cell_map, _params(),
            top_k=2)
        names = [a["target"] for a in ladder["alternates"]]
        self.assertEqual(names, ["big", "small"])


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
