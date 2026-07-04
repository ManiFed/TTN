"""Gauntlet: plan validation & timing contracts (F2, F3, F4, F19).

The cloud plans a night; the node must execute it faithfully:
time-series fields survive validation, start times are honored across the
whole night, plans wait for darkness, and interrupts obey the same bounds
as plans.
"""

import queue
import time
import types
import unittest
from unittest.mock import patch

from src import telemetry
from tests.gauntlet.util import TempCwdTestCase

import src.dashboard as dashboard


def _valid_item(**over):
    item = {
        "target": "V1234 Cyg", "ra": 20.5, "dec": 43.2,
        "expDur": 30, "expCount": 10, "binning": 1,
        "startTime": "01:30",
        "observation_mode": "time_series", "duration_minutes": 90,
        "filter": "CV", "notes": "transit",
    }
    item.update(over)
    return item


class ValidatorContractTest(unittest.TestCase):
    """F4/F19 — the validator must sanitize without silently degrading science."""

    def test_time_series_fields_survive_validation(self):
        valid, err = dashboard._validate_schedule_items([_valid_item()])
        self.assertIsNone(err)
        self.assertEqual(valid[0]["observation_mode"], "time_series")
        self.assertEqual(valid[0]["duration_minutes"], 90.0)
        self.assertEqual(valid[0]["filter"], "CV")

    def test_single_epoch_defaults_when_fields_absent(self):
        item = _valid_item()
        del item["observation_mode"], item["duration_minutes"]
        valid, err = dashboard._validate_schedule_items([item])
        self.assertIsNone(err)
        self.assertEqual(valid[0]["observation_mode"], "single_epoch")
        self.assertEqual(valid[0]["duration_minutes"], 0.0)

    def test_unknown_observation_mode_rejected(self):
        _, err = dashboard._validate_schedule_items(
            [_valid_item(observation_mode="stare_forever")])
        self.assertIn("observation_mode", err)

    def test_absurd_duration_rejected(self):
        _, err = dashboard._validate_schedule_items(
            [_valid_item(duration_minutes=100000)])
        self.assertIn("duration_minutes", err)

    def test_garbage_coordinates_rejected(self):
        _, err = dashboard._validate_schedule_items([_valid_item(ra=25.0)])
        self.assertIn("RA", err)
        _, err = dashboard._validate_schedule_items([_valid_item(dec=-95)])
        self.assertIn("Dec", err)


class StartTimeContractTest(unittest.TestCase):
    """F3 — items later tonight must be waited for in full, not run early."""

    @staticmethod
    def _now(hour, minute):
        return time.struct_time((2026, 7, 4, hour, minute, 0, 5, 185, -1))

    def test_item_seven_hours_ahead_waits_in_full(self):
        # 20:00 now, item at 03:00 → wait 7 h (the old 2 h cap ran it at 20:00)
        wait = dashboard._start_wait_seconds("03:00", self._now(20, 0))
        self.assertAlmostEqual(wait, 7 * 3600, delta=1)

    def test_recently_overdue_item_runs_immediately(self):
        # 03:10 now, item was 03:00 → overdue 10 min → run now
        self.assertEqual(
            dashboard._start_wait_seconds("03:00", self._now(3, 10)), 0.0)

    def test_item_far_in_past_runs_immediately(self):
        # 05:00 now, item was 22:00 (7 h ago) → overdue → run now
        self.assertEqual(
            dashboard._start_wait_seconds("22:00", self._now(5, 0)), 0.0)

    def test_unparseable_start_time_is_ignored(self):
        self.assertEqual(dashboard._start_wait_seconds("dusk", self._now(20, 0)), 0.0)
        self.assertEqual(dashboard._start_wait_seconds("25:99", self._now(20, 0)), 0.0)


class _FakeSafety:
    """Programmable stand-in for SafetyManager."""

    def __init__(self, sun=-30.0, safe=True, reason=""):
        self.sun = sun
        self.safe = safe
        self.reason = reason

    def status(self):
        return {"sun_elevation": self.sun, "dawn_threshold": -18.0,
                "safe": self.safe, "reason": self.reason}

    def is_safe(self):
        return self.safe


class DarknessGateTest(unittest.TestCase):
    """F2 — cloud plans delivered in daylight must wait, not burn."""

    def setUp(self):
        telemetry.reset_for_tests()
        self._orig_safety = dashboard._safety_mgr
        with dashboard._sched_lock:
            dashboard._sched_state.update(running=False, cancelled=False)

    def tearDown(self):
        dashboard._safety_mgr = self._orig_safety
        with dashboard._sched_lock:
            dashboard._sched_state.update(running=False, cancelled=False)
        telemetry.reset_for_tests()

    def test_already_dark_passes_immediately(self):
        dashboard._safety_mgr = _FakeSafety(sun=-30.0)
        self.assertTrue(dashboard._wait_for_darkness())

    def test_daylight_waits_until_sun_sets(self):
        fake = _FakeSafety(sun=+20.0)
        dashboard._safety_mgr = fake
        calls = {"n": 0}

        def fast_sleep(_s):
            calls["n"] += 1
            if calls["n"] > 5:
                fake.sun = -25.0  # sunset

        with patch("src.dashboard.time.sleep", side_effect=fast_sleep):
            self.assertTrue(dashboard._wait_for_darkness())
        self.assertGreater(calls["n"], 5)

    def test_daylight_dawn_latch_also_clears(self):
        fake = _FakeSafety(sun=+20.0, safe=False, reason="dawn — sun above threshold")
        dashboard._safety_mgr = fake

        def fast_sleep(_s):
            fake.sun = -25.0
            fake.safe = True
            fake.reason = ""

        with patch("src.dashboard.time.sleep", side_effect=fast_sleep):
            self.assertTrue(dashboard._wait_for_darkness())

    def test_cancellation_aborts_the_wait(self):
        dashboard._safety_mgr = _FakeSafety(sun=+20.0)
        with dashboard._sched_lock:
            dashboard._sched_state["cancelled"] = True
        self.assertFalse(dashboard._wait_for_darkness())

    def test_no_location_data_does_not_block(self):
        fake = _FakeSafety()
        fake.sun = None
        dashboard._safety_mgr = fake
        self.assertTrue(dashboard._wait_for_darkness())


class InterruptContractTest(TempCwdTestCase):
    """F19 — interrupts obey the same bounds as plans."""

    def setUp(self):
        super().setUp()
        telemetry.reset_for_tests()
        self.write("config.yaml", "observatory:\n  latitude: 31.0\n  longitude: -99.0\n")
        # Drain the interrupt queue
        while not dashboard._interrupt_queue.empty():
            dashboard._interrupt_queue.get_nowait()

    def tearDown(self):
        while not dashboard._interrupt_queue.empty():
            dashboard._interrupt_queue.get_nowait()
        telemetry.reset_for_tests()
        super().tearDown()

    def test_valid_interrupt_is_queued_normalized(self):
        dashboard._on_cloud_interrupt(
            {"name": "N Cyg 2026", "ra": 20.9, "dec": 41.0,
             "mag": 8.0, "time_critical": False})
        item = dashboard._interrupt_queue.get_nowait()
        self.assertEqual(item["target"], "N Cyg 2026")
        self.assertEqual(item["observation_mode"], "single_epoch")

    def test_out_of_bounds_interrupt_is_rejected_with_evidence(self):
        dashboard._on_cloud_interrupt(
            {"name": "Evil", "ra": 99.0, "dec": 200.0, "mag": 8.0})
        self.assertTrue(dashboard._interrupt_queue.empty())
        self.assertEqual(telemetry.counters().get("interrupt_rejected"), 1)

    def test_missing_coordinates_interrupt_is_dropped(self):
        dashboard._on_cloud_interrupt({"name": "NoCoords", "mag": 8.0})
        self.assertTrue(dashboard._interrupt_queue.empty())


class CloudPlanFlowTest(TempCwdTestCase):
    """F2/F15 — plan arrival leaves evidence and honors auto_run_plans."""

    def setUp(self):
        super().setUp()
        telemetry.reset_for_tests()
        self._orig_cloud = dashboard._cloud
        self._orig_safety = dashboard._safety_mgr
        with dashboard._sched_lock:
            dashboard._sched_state.update(running=False, cancelled=False)

    def tearDown(self):
        dashboard._cloud = self._orig_cloud
        dashboard._safety_mgr = self._orig_safety
        with dashboard._sched_lock:
            dashboard._sched_state.update(running=False, cancelled=False)
        telemetry.reset_for_tests()
        super().tearDown()

    def test_invalid_plan_rejected_with_evidence(self):
        dashboard._on_cloud_plan([{"target": "X", "ra": 99, "dec": 0}])
        self.assertEqual(telemetry.counters().get("plan_rejected"), 1)

    def test_auto_run_off_defers_plan_with_evidence(self):
        self.write("config.yaml",
                   "observatory:\n  latitude: 31.0\n  longitude: -99.0\n"
                   "cloud:\n  auto_run_plans: false\n")
        dashboard._cloud = types.SimpleNamespace(status={})
        dashboard._on_cloud_plan([_valid_item()])
        self.assertTrue(dashboard._cloud.status["plan_pending_review"])
        self.assertEqual(
            telemetry.counters().get("plan_deferred_auto_run_off"), 1)
        with dashboard._sched_lock:
            self.assertFalse(dashboard._sched_state["running"])

    def test_new_plan_supersedes_one_waiting_for_dark(self):
        self.write("config.yaml",
                   "observatory:\n  latitude: 31.0\n  longitude: -99.0\n"
                   "cloud:\n  auto_run_plans: true\n")
        dashboard._cloud = types.SimpleNamespace(status={})
        dashboard._safety_mgr = _FakeSafety(sun=-30.0)  # dark → runs immediately

        # Simulate an earlier cloud schedule stuck waiting for darkness…
        with dashboard._sched_lock:
            dashboard._sched_state.update(
                running=True, cancelled=False, source="cloud",
                current_phase="waiting_for_dark")

        # …that unwinds shortly after being cancelled (as the real one does).
        import threading

        def unwind():
            for _ in range(100):
                with dashboard._sched_lock:
                    if dashboard._sched_state["cancelled"]:
                        dashboard._sched_state["running"] = False
                        return
                time.sleep(0.05)

        threading.Thread(target=unwind, daemon=True).start()

        dashboard._on_cloud_plan([])  # empty valid plan: runs and finishes fast
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if telemetry.counters().get("schedule_finished"):
                break
            time.sleep(0.05)
        self.assertGreaterEqual(telemetry.counters().get("schedule_started", 0), 1)
        self.assertGreaterEqual(telemetry.counters().get("schedule_finished", 0), 1)


if __name__ == "__main__":
    unittest.main()
