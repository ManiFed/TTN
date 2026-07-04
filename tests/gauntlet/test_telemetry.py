"""Gauntlet: structured telemetry must survive every abuse (F14, F15).

Evidence is only useful if producing it can never crash the operation being
evidenced, and if it survives restarts and unreliable cloud links.
"""

import json
import os
import pathlib
import unittest

from src import telemetry
from tests.gauntlet.util import TempCwdTestCase


class TelemetryEventTest(TempCwdTestCase):
    def setUp(self):
        super().setUp()
        telemetry.reset_for_tests()

    def tearDown(self):
        telemetry.reset_for_tests()
        super().tearDown()

    def test_event_recorded_in_ring_counters_and_disk(self):
        telemetry.event("slew_failed", severity="error",
                        target="V1234 Cyg", detail={"timeout_s": 180})
        recent = telemetry.recent(10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["event"], "slew_failed")
        self.assertEqual(recent[0]["severity"], "error")
        self.assertEqual(telemetry.counters()["slew_failed"], 1)

        lines = pathlib.Path("logs", "events.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["event"], "slew_failed")

    def test_invalid_severity_is_coerced_not_crashed(self):
        entry = telemetry.event("weird", severity="catastrophic")
        self.assertEqual(entry["severity"], "info")

    def test_heartbeat_summary_carries_warning_and_worse_only(self):
        telemetry.event("plan_received", severity="info")
        telemetry.event("slew_failed", severity="error", target="T1")
        telemetry.event("photometry_queue_full", severity="warning")
        summary = telemetry.heartbeat_summary()
        names = [e["event"] for e in summary["recent"]]
        self.assertIn("slew_failed", names)
        self.assertIn("photometry_queue_full", names)
        self.assertNotIn("plan_received", names)
        # counters keep the full picture, including info events
        self.assertEqual(summary["counters"]["plan_received"], 1)
        self.assertIn("uptime_s", summary)

    def test_forwarder_gets_error_and_critical_only(self):
        forwarded = []
        done = __import__("threading").Event()

        def fw(entry):
            forwarded.append(entry["event"])
            done.set()

        telemetry.set_forwarder(fw)
        telemetry.event("plan_received", severity="info")
        telemetry.event("emergency_park", severity="critical")
        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(forwarded, ["emergency_park"])

    def test_forwarder_exception_never_propagates(self):
        fired = __import__("threading").Event()

        def bad_forwarder(entry):
            fired.set()
            raise RuntimeError("cloud unreachable")

        telemetry.set_forwarder(bad_forwarder)
        telemetry.event("exposure_failed", severity="error")
        self.assertTrue(fired.wait(timeout=5))
        # And the next event still records normally.
        telemetry.event("exposure_failed", severity="error")
        self.assertEqual(telemetry.counters()["exposure_failed"], 2)

    def test_events_file_write_failure_is_swallowed(self):
        # Make logs/ a *file* so the JSONL append fails with OSError.
        with open("logs", "w") as fh:
            fh.write("not a directory")
        entry = telemetry.event("disk_low", severity="warning")
        self.assertEqual(entry["event"], "disk_low")  # no exception raised

    def test_corrupt_events_file_partially_loads(self):
        os.makedirs("logs", exist_ok=True)
        with open(os.path.join("logs", "events.jsonl"), "w") as fh:
            fh.write('{"event": "good", "severity": "info"}\n')
            fh.write("{{{{ corrupt line\n")
            fh.write('{"event": "also_good", "severity": "info"}\n')
        events = telemetry.load_events_file()
        self.assertEqual([e["event"] for e in events], ["good", "also_good"])

    def test_events_file_rotates_instead_of_growing_forever(self):
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", "events.jsonl")
        with open(path, "w") as fh:
            fh.write("x" * (telemetry._EVENTS_FILE_MAX_BYTES + 1))
        telemetry.event("node_started")
        self.assertTrue(os.path.exists(path + ".1") or
                        os.path.exists(os.path.join("logs", "events.jsonl.1")))
        self.assertLess(os.path.getsize(path), 10_000)


if __name__ == "__main__":
    unittest.main()
