"""Deterministic smoke tests for the fuzz harness itself.

Two jobs:
  1. Prove every invariant CAN fire (a checker that never fails a broken
     input would silently bless every fuzz run).
  2. Prove fault-plan generation is deterministic (replayability).

One full-harness subprocess run is included (marked slow-ish, ~30 s); the
rest are unit-level and fast. Runs under `make gauntlet`/`make fuzz-smoke`.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

from tests.fuzz import invariants as inv
from tests.fuzz.faults import FaultPlan


class InvariantsCanFireTest(unittest.TestCase):
    """Each checker must reject a synthetic broken state."""

    def test_thread_death_detected(self):
        v = inv.check_threads(set(), [{"thread": "poller", "type": "KeyError",
                                       "value": "'ra'"}])
        self.assertTrue(v)

    def test_wedged_schedule_detected(self):
        v = inv.check_phase({"running": True, "current_phase": "slewing"})
        self.assertTrue(any("wedged" in p for p in v))

    def test_unknown_phase_detected(self):
        v = inv.check_phase({"running": False, "current_phase": "confused"})
        self.assertTrue(any("unknown" in p for p in v))

    def test_unsafe_without_park_detected(self):
        v = inv.check_safety_parked({"safe": False, "reason": "disconnect"}, 0)
        self.assertTrue(v)

    def test_unsafe_with_park_attempt_passes(self):
        v = inv.check_safety_parked({"safe": False, "reason": "disconnect"}, 2)
        self.assertFalse(v)

    def test_unexplained_traceback_detected(self, tmp=None):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "node.log"
            log.write_text(
                "2026-01-01 ERROR boom\nTraceback (most recent call last):\n"
                '  File "x.py", line 1, in f\n'
                "ZeroDivisionError: division by zero\n")
            self.assertTrue(inv.check_log(log))

    def test_allowlisted_traceback_passes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "node.log"
            log.write_text(
                "Traceback (most recent call last):\n"
                '  File "x.py", line 1, in f\n'
                "alpaca.client.AlpacaError: park → ErrorNumber 1031\n")
            self.assertFalse(inv.check_log(log))

    def test_outbox_loss_detected(self):
        class _FC:
            def count_paths(self, needle):
                return 1
        v = inv.check_outbox(_FC(), Path("/nonexistent"), enqueued=5)
        self.assertTrue(v)


class FaultPlanDeterminismTest(unittest.TestCase):
    def test_same_seed_same_plan(self):
        a = FaultPlan.generate(1234, 30, "mixed").to_json()
        b = FaultPlan.generate(1234, 30, "mixed").to_json()
        self.assertEqual(a, b)

    def test_roundtrip(self):
        plan = FaultPlan.generate(7, 30, "heavy")
        again = FaultPlan.from_json(plan.to_json())
        self.assertEqual(plan.to_json(), again.to_json())

    def test_none_profile_is_clean(self):
        plan = FaultPlan.generate(99, 30, "none")
        self.assertEqual((plan.p_transport, plan.p_protocol, plan.p_semantic,
                          plan.episodes), (0.0, 0.0, 0.0, []))


class FullHarnessBaselineTest(unittest.TestCase):
    """One real subprocess run, zero faults: everything must hold."""

    def test_no_fault_baseline_passes(self):
        result_file = Path(self.enterContext(
            __import__("tempfile").TemporaryDirectory())) / "r.json"
        proc = subprocess.run(
            [sys.executable, "-m", "tests.fuzz.runner", "--one", "0",
             "--profile", "none", "--scenario-s", "12",
             "--result-file", str(result_file)],
            cwd=REPO, capture_output=True, timeout=150)
        self.assertTrue(result_file.exists(),
                        f"no result: {proc.stderr.decode(errors='replace')[-1500:]}")
        result = json.loads(result_file.read_text())
        self.assertEqual(result["violations"], [],
                         f"baseline violations: {result['violations']}")
        self.assertEqual(result["sched_state"]["completed"], 2)


if __name__ == "__main__":
    unittest.main()
