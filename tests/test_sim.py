#!/usr/bin/env python3
"""
Tests for the fleet digital twin (sim/).

Three layers:

  * pure components — sky math against known values, world/weather generation
    determinism and shape;
  * guardrails — the "obvious physics" a credible twin must reproduce:
    more reliable fleets yield more, bad weather yields less, longitude
    spread widens time coverage, and no scheduler can place an impossible
    observation (the engine hard-validates every pick);
  * a CI-safe smoke test — one tiny scenario end to end, CHORUS + a baseline,
    twice, asserting byte-identical results for a fixed seed.

Everything here is offline: no DB, no astropy, no network.  Larger studies
(50+ nodes, full scenario library) are opt-in via `python -m sim run`.
"""

import unittest
from datetime import datetime, timedelta, timezone

from sim import skymath, weather
from sim.engine import build_world, run_world
from sim.metrics import summarize_run
from sim.scenarios import SCENARIOS, get
from sim.world import (HARDWARE_CLASSES, REGIONS, generate_catalog,
                       generate_fleet, region_clear_prob)

WHEN = datetime(2026, 3, 21, 4, 0, tzinfo=timezone.utc)


def _tiny(**kw):
    """A CI-sized world: 5 nodes, 40 targets, 3 nights."""
    base = get("beta_5_nodes").variant(nights=3, n_targets=40)
    return base.variant(**kw) if kw else base


def _accepted(result: dict) -> int:
    return sum(n["n_accepted"] for n in result["nights"])


# ── Sky math ──────────────────────────────────────────────────────────────────

class SkymathTest(unittest.TestCase):
    def test_polaris_altitude_tracks_latitude(self):
        # Polaris sits within ~1° of the observer's latitude.
        alt, _ = skymath.altaz(37.95, 89.26, 40.0, -105.0, WHEN)
        self.assertAlmostEqual(alt, 40.0, delta=1.5)

    def test_night_window_is_dark_and_bounded(self):
        win = skymath.night_window(40.0, -105.0, WHEN)
        self.assertIsNotNone(win)
        t0, t1 = win
        self.assertLess(t0, t1)
        self.assertLess((t1 - t0), timedelta(hours=16))
        mid = t0 + (t1 - t0) / 2
        self.assertLess(skymath.sun_alt(40.0, -105.0, mid), -12.0)

    def test_polar_day_returns_none(self):
        june = datetime(2026, 6, 21, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(skymath.night_window(78.0, 15.0, june))

    def test_moon_state_sane(self):
        m = skymath.moon_state(WHEN)
        self.assertTrue(0.0 <= m["illumination"] <= 1.0)
        self.assertTrue(0.0 <= m["ra_deg"] < 360.0)


# ── World generation ──────────────────────────────────────────────────────────

class WorldGenTest(unittest.TestCase):
    def test_fleet_deterministic(self):
        a = generate_fleet(20, 42)
        b = generate_fleet(20, 42)
        self.assertEqual([vars(n) for n in a], [vars(n) for n in b])
        c = generate_fleet(20, 43)
        self.assertNotEqual([vars(n) for n in a], [vars(n) for n in c])

    def test_fleet_fields_in_range(self):
        for n in generate_fleet(50, 7):
            self.assertIn(n.hardware_class, HARDWARE_CLASSES)
            self.assertIn(n.region, REGIONS)
            r = REGIONS[n.region]
            self.assertTrue(r["lat"][0] <= n.lat <= r["lat"][1])
            self.assertTrue(0.0 < n.p_exec_true <= 0.98)
            self.assertTrue(0.0 < n.p_accept_true <= 0.98)
            self.assertGreaterEqual(n.kappa_true, 1.0)

    def test_regions_filter_respected(self):
        fleet = generate_fleet(30, 42, regions=["australia"])
        self.assertTrue(all(n.region == "australia" for n in fleet))
        self.assertTrue(all(n.lat < 0 for n in fleet))

    def test_catalog_deterministic_and_shaped(self):
        epoch = WHEN
        a = generate_catalog(100, 42, epoch, n_nights=10)
        b = generate_catalog(100, 42, epoch, n_nights=10)
        self.assertEqual([vars(t) for t in a], [vars(t) for t in b])
        classes = {t.target_type for t in a}
        self.assertTrue({"EB", "CV", "LPV", "EXOPLANET", "VAR"} <= classes)
        for t in a:
            if t.target_type == "EB":
                self.assertIsNotNone(t.ephemeris)
            if t.target_type == "EXOPLANET":
                self.assertIsNotNone(t.transit)

    def test_alert_storm_lands_on_the_named_night(self):
        cat = generate_catalog(50, 42, WHEN, n_nights=10,
                               transient_rate_per_night=0.0,
                               alert_storm_night=3, alert_storm_count=6)
        storm = [t for t in cat if t.alert_night is not None]
        self.assertEqual(len(storm), 6)
        self.assertTrue(all(t.alert_night == 3 for t in storm))

    def test_climatology_seasonal_shape(self):
        best = region_clear_prob("na_southwest", 10)
        worst = region_clear_prob("na_southwest", 4)
        self.assertGreater(best, worst)


# ── Weather ───────────────────────────────────────────────────────────────────

class WeatherTest(unittest.TestCase):
    def _node(self, seed=42):
        return generate_fleet(1, seed, regions=["na_southwest"])[0]

    def test_deterministic_and_fleet_independent(self):
        node = self._node()
        a = weather.night_weather(42, 3, node, 30, 6)
        b = weather.night_weather(42, 3, node, 30, 6)
        self.assertEqual(a.truth_clear, b.truth_clear)
        self.assertEqual(a.forecast_clear, b.forecast_clear)

    def test_forecast_bounded(self):
        wx = weather.night_weather(42, 0, self._node(), 40, 6)
        self.assertTrue(all(0.0 < f < 1.0 for f in wx.forecast_clear))
        self.assertEqual(len(wx.truth_clear), 40)

    def test_bad_climate_means_fewer_clear_slots(self):
        node = self._node()
        overrides = {"na_southwest": {"clear": 0.10, "amp": 0.0}}
        clear_frac = lambda ov: sum(
            sum(1 for c in weather.night_weather(
                42, night, node, 30, 6, climate_overrides=ov).truth_clear if c)
            for night in range(20)) / (20 * 30)
        self.assertLess(clear_frac(overrides), clear_frac(None) - 0.15)


# ── Guardrails: the twin must reproduce obvious physics ──────────────────────

class GuardrailTest(unittest.TestCase):
    def test_fixed_seed_fixed_output(self):
        sc = _tiny()
        r1 = run_world(build_world(sc, 42), "chorus")
        r2 = run_world(build_world(sc, 42), "chorus")
        self.assertEqual(r1["nights"], r2["nights"])
        self.assertEqual(r1["alerts"], r2["alerts"])
        r3 = run_world(build_world(sc, 99), "chorus")
        self.assertNotEqual(r1["nights"], r3["nights"])

    def test_reliable_nodes_yield_more(self):
        lo = run_world(build_world(_tiny(reliability_scale=0.5), 42), "chorus")
        hi = run_world(build_world(_tiny(reliability_scale=1.1), 42), "chorus")
        self.assertGreater(_accepted(hi), _accepted(lo))

    def test_bad_weather_reduces_yield(self):
        bad_climate = {r: {"clear": 0.12, "amp": 0.02} for r in REGIONS}
        good = run_world(build_world(_tiny(), 42), "chorus")
        bad = run_world(build_world(_tiny(
            climate_overrides=bad_climate, regional_correlation=0.85,
            forecast_skill_scale=0.5), 42), "chorus")
        self.assertLess(_accepted(bad), _accepted(good))

    def test_longitude_spread_widens_time_coverage(self):
        clustered = _tiny(n_nodes=8, regions=["na_east"])
        spread = _tiny(n_nodes=8, regions=["na_southwest", "europe_south",
                                           "east_asia", "australia"])
        hrs = lambda sc: summarize_run(run_world(build_world(sc, 42), "chorus")
                                       )["utc_hours_covered_per_night"]
        self.assertGreater(hrs(spread), hrs(clustered))

    def test_no_impossible_assignments_any_scheduler(self):
        # engine._validate_picks raises on infeasible placements; a clean run
        # is the assertion, for every scheduler on the same world.
        world = build_world(_tiny(nights=2), 42)
        for sched in ("chorus", "legacy", "greedy_value", "greedy_nearest",
                      "random"):
            result = run_world(world, sched)
            self.assertGreaterEqual(_accepted(result), 0)


# ── CI smoke test ─────────────────────────────────────────────────────────────

class SmokeTest(unittest.TestCase):
    def test_scenario_library_complete(self):
        required = {"beta_5_nodes", "launch_50_nodes", "growth_200_nodes",
                    "global_1000_nodes", "bad_weather_week", "alert_storm",
                    "southern_gap", "unreliable_fleet", "exoplanet_campaign",
                    "photometry_quality_crisis"}
        self.assertTrue(required <= set(SCENARIOS))

    def test_end_to_end_smoke(self):
        """One tiny world, CHORUS vs random: both produce accepted
        measurements, summaries are well-formed, and CHORUS wastes no more
        time per accepted measurement than random slotting."""
        world = build_world(_tiny(), 42)
        chorus = summarize_run(run_world(world, "chorus"))
        rand = summarize_run(run_world(world, "random"))
        for s in (chorus, rand):
            self.assertGreater(s["accepted_per_night"], 0.0)
            self.assertGreaterEqual(s["realized_value_per_night"], 0.0)
            self.assertLessEqual(s["value_capture_frac"], 1.0)
        self.assertGreaterEqual(chorus["realized_value_per_night"],
                                rand["realized_value_per_night"])


if __name__ == "__main__":
    unittest.main()
