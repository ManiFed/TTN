#!/usr/bin/env python3
"""
Node-side never-idle logic — pure helpers from src.dashboard, no devices:

  * _pick_gap_filler / _estimated_dwell_s — which alternate (if any) fits a
    dead wait before a scheduled item, including the time-series margin,
  * _store_alternates / _next_alternate — the plan's contingency ladder is
    validated, ranked by expected_info, and each alternate runs at most once,
  * the work_starved flag rises only via _mark_work_starved and clears on a
    new plan.

Run with:  python3 -m unittest tests.test_never_idle_node
"""

import unittest

from src import dashboard


def _alt(target="ALT-1", ra=10.0, dec=40.0, exp_dur=30.0, exp_count=10,
         expected_info=0.5, **kw):
    a = {"target": target, "ra": ra, "dec": dec, "expDur": exp_dur,
         "expCount": exp_count, "binning": 1, "filter": "CV",
         "expected_info": expected_info}
    a.update(kw)
    return a


class GapFillerTest(unittest.TestCase):
    def test_estimated_dwell_includes_slew_margin(self):
        # 30 s × 10 frames + the slew/settle margin.
        self.assertAlmostEqual(
            dashboard._estimated_dwell_s({"expDur": 30, "expCount": 10}),
            300.0 + dashboard._GAP_FILL_MARGIN_S)

    def test_estimated_dwell_time_series_uses_full_window(self):
        item = {"expDur": 10, "expCount": 5,
                "observation_mode": "time_series", "duration_minutes": 45.0}
        self.assertAlmostEqual(
            dashboard._estimated_dwell_s(item),
            45.0 * 60.0 + dashboard._GAP_FILL_MARGIN_S)

    def test_pick_gap_filler_fits(self):
        alts = [_alt(exp_dur=30, exp_count=10)]   # 300 s + margin
        gap = 300.0 + dashboard._GAP_FILL_MARGIN_S + 1.0
        self.assertEqual(dashboard._pick_gap_filler(alts, gap, set()), 0)

    def test_pick_gap_filler_rejects_too_long(self):
        alts = [_alt(exp_dur=30, exp_count=100)]  # 3000 s + margin
        self.assertIsNone(dashboard._pick_gap_filler(alts, 900.0, set()))

    def test_pick_gap_filler_skips_used_and_falls_through(self):
        alts = [_alt(target="A", exp_dur=30, exp_count=10),
                _alt(target="B", exp_dur=10, exp_count=10)]
        gap = 300.0 + dashboard._GAP_FILL_MARGIN_S + 1.0
        self.assertEqual(dashboard._pick_gap_filler(alts, gap, {0}), 1)

    def test_pick_gap_filler_none_when_all_used(self):
        alts = [_alt()]
        self.assertIsNone(dashboard._pick_gap_filler(alts, 10_000.0, {0}))


class AlternatesQueueTest(unittest.TestCase):
    def setUp(self):
        dashboard._store_alternates({})   # reset module state

    def tearDown(self):
        dashboard._store_alternates({})

    def test_alternates_validated_ranked_and_consumed_once(self):
        dashboard._store_alternates({"alternates": [
            _alt(target="LOW", expected_info=0.1),
            _alt(target="HIGH", expected_info=0.9),
            {"target": "BAD", "ra": 99.0, "dec": 40.0},   # RA out of range
        ]})
        first = dashboard._next_alternate()
        second = dashboard._next_alternate()
        self.assertEqual(first["target"], "HIGH")     # ranked by value
        self.assertEqual(second["target"], "LOW")
        self.assertIsNone(dashboard._next_alternate())  # each runs once

    def test_next_alternate_with_gap_constraint(self):
        dashboard._store_alternates({"alternates": [
            _alt(target="LONG", exp_dur=30, exp_count=100, expected_info=0.9),
            _alt(target="SHORT", exp_dur=10, exp_count=6, expected_info=0.1),
        ]})
        # Only SHORT (60 s + margin) fits a ~5-minute gap.
        picked = dashboard._next_alternate(gap_s=300.0)
        self.assertEqual(picked["target"], "SHORT")

    def test_new_plan_replaces_alternates_wholesale(self):
        dashboard._store_alternates({"alternates": [_alt(target="OLD")]})
        dashboard._store_alternates({"alternates": [_alt(target="NEW")]})
        self.assertEqual(dashboard._next_alternate()["target"], "NEW")
        self.assertIsNone(dashboard._next_alternate())


class WorkStarvedFlagTest(unittest.TestCase):
    def setUp(self):
        dashboard._work_starved.clear()

    def tearDown(self):
        dashboard._work_starved.clear()

    def test_mark_sets_flag_once(self):
        self.assertFalse(dashboard._work_starved.is_set())
        dashboard._mark_work_starved()
        self.assertTrue(dashboard._work_starved.is_set())
        dashboard._mark_work_starved()   # idempotent
        self.assertTrue(dashboard._work_starved.is_set())


if __name__ == "__main__":
    unittest.main()
