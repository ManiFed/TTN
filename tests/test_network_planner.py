#!/usr/bin/env python3
"""
Tests for the network-level scheduling optimizer.

These exercise the pure coordination logic (objective marginal-value model,
global assignment, sequencing, feasibility helpers, tuning param clamping)
without a database or astropy, by constructing opportunities/contexts directly.
"""

import unittest
from datetime import datetime, timedelta, timezone

from cloud import objective, tuning
from cloud.conditions import horizon_min_alt
from cloud.network_planner import (
    NodeContext, Opportunity, STEP_MIN, assign_network, needs_meridian_flip,
    sequence_node, _target_groups, _Placement,
)

BASE = datetime(2026, 6, 27, 22, 0, tzinfo=timezone.utc)


def _ctx(node_id, lon, max_targets=1, n_slots=16):
    return NodeContext(
        node={}, node_id=node_id, lat=0.0, lon=lon,
        t0=BASE, t1=BASE + timedelta(minutes=STEP_MIN * n_slots),
        n_slots=n_slots, utc_offset=timedelta(0), min_alt=25.0,
        horizon_mask=[], filters=["CV"], cooled=False, mount_type="alt_az",
        cloud_relax=0.0, max_targets=max_targets)


def _opp(node_id, target_id, base, lon, slots=(0, 1, 2, 3), **kw):
    return Opportunity(
        node_id=node_id, target_id=target_id, name=target_id,
        ra_deg=kw.get("ra_deg", 10.0), dec_deg=kw.get("dec_deg", 0.0),
        mag=12.0, target_type=kw.get("target_type", "VAR"), base_value=base,
        exp_dur=10.0, exp_count=30, need=kw.get("need", 1),
        feasible={s: 1.0 for s in slots},
        az_by_slot=kw.get("az_by_slot", {s: 90.0 for s in slots}),
        filter="CV", longitude=lon, cadence_hours=kw.get("cadence_hours", 24.0),
        is_transit=kw.get("is_transit", False), pinned_slot=kw.get("pinned_slot"),
        duration_min=5.0)


# ── Objective: redundancy + cadence (the coordination engine) ────────────────

class ObjectiveModelTest(unittest.TestCase):
    def test_redundancy_first_obs_full_value(self):
        self.assertEqual(objective.redundancy_factor(0, 0.0), 1.0)

    def test_redundancy_strictly_decreases_with_repeats(self):
        f1 = objective.redundancy_factor(1, 0.0)
        f2 = objective.redundancy_factor(2, 0.0)
        f3 = objective.redundancy_factor(3, 0.0)
        self.assertLess(f1, 1.0)
        self.assertLess(f2, f1)
        self.assertLess(f3, f2)

    def test_redundancy_rewards_longitude_spread(self):
        clustered = objective.redundancy_factor(1, 0.0)
        spread = objective.redundancy_factor(1, 120.0)
        self.assertGreater(spread, clustered)
        self.assertLess(spread, 1.0)   # still a penalized repeat

    def test_cadence_first_sample_full_value(self):
        self.assertEqual(objective.cadence_bonus(BASE, [], 24.0), 1.0)

    def test_cadence_high_for_empty_bin_low_for_clustered(self):
        far = objective.cadence_bonus(BASE + timedelta(hours=24), [BASE], 24.0)
        near = objective.cadence_bonus(BASE + timedelta(minutes=5), [BASE], 24.0)
        self.assertGreater(far, 0.9)
        self.assertLess(near, 0.1)     # clustered repeat suppressed at strength 1

    def test_slot_quality_normalization_floor_and_peak(self):
        norm = objective.normalize_slot_quality({0: 1.0, 1: 0.5, 2: 0.0}, floor=0.5)
        self.assertAlmostEqual(norm[0], 1.0)
        self.assertAlmostEqual(norm[1], 0.75)
        self.assertAlmostEqual(norm[2], 0.5)


# ── Sequencing geometry (slew / meridian flip) ───────────────────────────────

class SequencingGeometryTest(unittest.TestCase):
    def test_two_opt_reduces_slew(self):
        pts = [(0.0, 0.0), (0.0, 10.0), (0.0, 20.0), (0.0, 30.0)]
        bad = [0, 2, 1, 3]
        bad_len = objective.path_length([pts[i] for i in bad])
        order = objective.two_opt_order(pts)
        opt_len = objective.path_length([pts[i] for i in order])
        self.assertLessEqual(opt_len + 1e-9, bad_len)
        self.assertAlmostEqual(opt_len, 30.0, places=3)   # the monotone tour

    def test_meridian_flip_only_equatorial_across_meridian(self):
        self.assertTrue(needs_meridian_flip("equatorial", 90.0, 270.0))
        self.assertFalse(needs_meridian_flip("equatorial", 90.0, 170.0))
        self.assertFalse(needs_meridian_flip("alt_az", 90.0, 270.0))


# ── Feasibility: local horizon mask ──────────────────────────────────────────

class HorizonMaskTest(unittest.TestCase):
    def test_empty_mask_is_unobstructed(self):
        self.assertEqual(horizon_min_alt([], 123.0), 0.0)

    def test_interpolates_between_points(self):
        mask = [[20.0, 0.0], [10.0, 90.0], [20.0, 180.0], [10.0, 270.0]]
        self.assertAlmostEqual(horizon_min_alt(mask, 45.0), 15.0, places=3)

    def test_wraps_across_360(self):
        mask = [[10.0, 350.0], [20.0, 10.0]]   # spans the 0° seam
        self.assertAlmostEqual(horizon_min_alt(mask, 0.0), 15.0, places=3)


# ── Global assignment: coordination ──────────────────────────────────────────

class AssignmentTest(unittest.TestCase):
    def _run(self, contexts, opps_by_node):
        return assign_network(
            contexts, opps_by_node,
            config={"scheduler": {"local_search_ms": 100}},
            params={"coordination": objective.DEFAULT_COORD_PARAMS},
            seed=7)

    def test_avoids_redundant_double_coverage(self):
        """Two nodes can both see one bright target; each also has a slightly
        weaker unique target. A base-value greedy would put the bright target on
        both nodes; the marginal-value optimizer must cover the bright target
        once and a unique target on the other node instead."""
        contexts = {"A": _ctx("A", 0.0, max_targets=1),
                    "B": _ctx("B", 120.0, max_targets=1)}
        opps = {
            "A": [_opp("A", "bright", 0.90, 0.0), _opp("A", "uniq_a", 0.70, 0.0)],
            "B": [_opp("B", "bright", 0.90, 120.0), _opp("B", "uniq_b", 0.70, 120.0)],
        }
        assignments, stats = self._run(contexts, opps)
        placed = [opp.target_id for lst in assignments.values() for opp, _ in lst]
        self.assertEqual(placed.count("bright"), 1, f"bright double-covered: {placed}")
        self.assertTrue(set(placed) & {"uniq_a", "uniq_b"},
                        f"no unique target covered: {placed}")

    def test_local_search_never_regresses(self):
        contexts = {"A": _ctx("A", 0.0, max_targets=3),
                    "B": _ctx("B", 120.0, max_targets=3)}
        opps = {
            "A": [_opp("A", f"a{i}", 0.5 + 0.05 * i, 0.0) for i in range(4)],
            "B": [_opp("B", f"b{i}", 0.5 + 0.05 * i, 120.0) for i in range(4)],
        }
        _, stats = self._run(contexts, opps)
        self.assertGreaterEqual(stats["final_objective"], stats["greedy_objective"])

    def test_deterministic(self):
        contexts = {"A": _ctx("A", 0.0, max_targets=2),
                    "B": _ctx("B", 120.0, max_targets=2)}
        opps = {
            "A": [_opp("A", "bright", 0.9, 0.0), _opp("A", "uniq_a", 0.7, 0.0)],
            "B": [_opp("B", "bright", 0.9, 120.0), _opp("B", "uniq_b", 0.7, 120.0)],
        }
        a1, _ = self._run(contexts, opps)
        a2, _ = self._run(contexts, opps)
        sig = lambda a: sorted((n, o.target_id, s) for n, lst in a.items() for o, s in lst)
        self.assertEqual(sig(a1), sig(a2))

    def test_transit_pinned_to_its_window(self):
        contexts = {"A": _ctx("A", 0.0, max_targets=5, n_slots=16)}
        transit = _opp("A", "planet", 0.9, 0.0, is_transit=True, pinned_slot=6, need=2)
        transit.feasible = {6: 1.0}
        opps = {"A": [transit, _opp("A", "var", 0.8, 0.0)]}
        assignments, _ = self._run(contexts, opps)
        slot_of = {o.target_id: s for o, s in assignments["A"]}
        self.assertEqual(slot_of["planet"], 6)


# ── Sequencing emits a valid, time-ordered plan with rationale ───────────────

class SequenceNodeTest(unittest.TestCase):
    def test_emits_ordered_items_with_rationale(self):
        ctx = _ctx("A", 0.0, max_targets=5)
        o1 = _opp("A", "t1", 0.8, 0.0, slots=(0,))
        o2 = _opp("A", "t2", 0.7, 0.0, slots=(4,))
        assigned = [(o2, 4), (o1, 0)]      # out of time order on purpose
        groups = _target_groups(
            [_Placement("A", o1, 0), _Placement("A", o2, 4)], {"A": ctx})
        items = sequence_node(ctx, assigned, objective.DEFAULT_COORD_PARAMS, groups)
        self.assertEqual([i.startTime for i in items], sorted(i.startTime for i in items))
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertIn("marginal_value", it.explanation)
            self.assertIn("network_redundancy_rank", it.explanation)


# ── Generalized AI tuning: param clamping safety ─────────────────────────────

class TuningParamsTest(unittest.TestCase):
    def test_canonical_fills_defaults(self):
        params = tuning._canonical({"composite": {"science": 0.9}})
        self.assertEqual(set(params), {"composite", "observability",
                                       "slot_quality", "coordination"})
        self.assertEqual(set(params["observability"]), set(tuning.OBS_KEYS))

    def test_weight_group_clamp_normalizes_and_bounds_step(self):
        cur = dict(tuning.DEFAULT_OBS_WEIGHTS)
        proposed = {k: (1.0 if k == "weather" else 0.0) for k in tuning.OBS_KEYS}
        new = tuning._clamp_and_normalize(
            cur, proposed, tuning.OBS_KEYS, tuning.DEFAULT_OBS_WEIGHTS, max_delta=0.05)
        self.assertAlmostEqual(sum(new.values()), 1.0, places=3)
        for k in tuning.OBS_KEYS:
            self.assertLessEqual(abs(new[k] - cur[k]), 0.05 + 1e-6)

    def test_coordination_clamp_respects_bounds_and_delta(self):
        cur = dict(tuning.DEFAULT_COORD_PARAMS)
        proposed = {k: 9999.0 for k in tuning.COORD_KEYS}   # absurd ask
        new = tuning._clamp_scalars(cur, proposed, max_delta_coord=0.15)
        for k in tuning.COORD_KEYS:
            lo, hi = tuning.COORD_BOUNDS[k]
            self.assertGreaterEqual(new[k], lo)
            self.assertLessEqual(new[k], hi)
            # never a giant jump in one run
            cap = max(0.15 * abs(cur[k]), 0.15 * (hi - lo) * 0.1)
            self.assertLessEqual(new[k] - cur[k], cap + 1e-6)

    def test_no_churn_guard(self):
        cur = tuning._canonical({})
        self.assertFalse(tuning._params_material(cur, cur, 0.005))


if __name__ == "__main__":
    unittest.main()
