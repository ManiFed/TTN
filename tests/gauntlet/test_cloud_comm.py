"""Gauntlet: CloudCommunicator vs. an unreliable cloud (F7, F10, F11).

Simulates outages, HTTP 500s, rejected registrations, crash-corrupted queue
files, and deep upload backlogs — asserting the node never loses data
silently, never hammers the API without backoff, and always leaves evidence.
"""

import json
import os
import pathlib
import unittest

from src import telemetry
from src.cloud_communicator import CloudCommunicator
from tests.gauntlet.fakecloud import FakeCloud
from tests.gauntlet.util import TempCwdTestCase


def _config(url: str) -> dict:
    return {
        "cloud": {
            "enabled": True,
            "url": url,
            "node_id": "",
            "api_key": "",
            "heartbeat_interval": 1,
            "plan_poll_interval": 1,
            "activation_code": "BS-2026-TESTCODE",
        },
        "observatory": {"latitude": 31.36, "longitude": -99.44,
                        "elevation": 500, "observer": "Gauntlet"},
        "photometry": {"node_id": "", "filter_name": "CV"},
    }


class CloudCommGauntletTest(TempCwdTestCase):
    def setUp(self):
        super().setUp()
        telemetry.reset_for_tests()
        self.fake = FakeCloud().start()

    def tearDown(self):
        self.fake.stop()
        telemetry.reset_for_tests()
        super().tearDown()

    def _comm(self, url=None) -> CloudCommunicator:
        return CloudCommunicator(_config(url or self.fake.url))

    # ── Registration ───────────────────────────────────────────────────────────

    def test_registration_success_persists_credentials(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        state = json.loads(pathlib.Path("data", "cloud_state.json").read_text())
        self.assertEqual(state["node_id"], "node_test01")
        self.assertNotIn("api_key", state)
        # A second instance (simulated restart) reuses them without re-registering.
        self.fake.clear()
        comm2 = self._comm()
        self.assertTrue(comm2._ensure_registered())
        self.assertEqual(self.fake.paths("/register"), [])

    def test_registration_failure_backs_off_instead_of_hammering(self):
        self.fake.mode = "reject"
        comm = self._comm()
        self.assertFalse(comm._ensure_registered())
        n_after_first = len(self.fake.paths("/register"))
        # Immediate retries are suppressed by the backoff window.
        for _ in range(10):
            self.assertFalse(comm._ensure_registered())
        self.assertEqual(len(self.fake.paths("/register")), n_after_first)
        self.assertGreater(comm._register_next_attempt, 0)
        # Structured evidence of the failure exists.
        self.assertGreaterEqual(telemetry.counters().get("registration_failed", 0), 1)

    def test_registration_outage_recovers_after_backoff(self):
        self.fake.mode = "http500"
        comm = self._comm()
        self.assertFalse(comm._ensure_registered())
        # Simulate the backoff window elapsing, then the cloud coming back.
        comm._register_next_attempt = 0.0
        self.fake.mode = "ok"
        self.assertTrue(comm._ensure_registered())

    # ── Upload queue ───────────────────────────────────────────────────────────

    def test_upload_failure_queues_to_disk_and_flushes_on_recovery(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        self.fake.mode = "http500"
        delivered = comm.submit_measurement(
            {"target_name": "T Tau", "magnitude": 10.2, "bjd": 2460000.5})
        self.assertFalse(delivered)
        queue = json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())
        self.assertEqual(len(queue), 1)

        self.fake.mode = "ok"
        self.fake.clear()
        comm._flush_queue()
        self.assertEqual(len(self.fake.paths("/measurements")), 1)
        queue = json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())
        self.assertEqual(queue, [])

    def test_unregistered_node_queues_measurements(self):
        self.fake.mode = "down"
        comm = self._comm()
        comm._register_next_attempt = float("inf")  # stay unregistered
        self.assertFalse(comm.submit_measurement({"target_name": "X", "bjd": 1.0}))
        queue = json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())
        self.assertEqual(len(queue), 1)

    def test_corrupt_queue_file_does_not_crash(self):
        comm = self._comm()
        os.makedirs("data", exist_ok=True)
        with open(os.path.join("data", "cloud_upload_queue.json"), "w") as fh:
            fh.write('[{"measurement": truncated-by-cras')
        self.assertEqual(comm._load_queue(), [])
        # Enqueueing on top of the corrupt file recovers it.
        comm._enqueue({"measurement": {"target_name": "Y"}})
        queue = json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())
        self.assertEqual(len(queue), 1)

    def test_queue_overflow_drops_oldest_and_leaves_evidence(self):
        comm = self._comm()
        from src import cloud_communicator as cc
        for i in range(cc._QUEUE_MAX + 5):
            comm._enqueue({"measurement": {"i": i}})
        queue = comm._load_queue()
        self.assertEqual(len(queue), cc._QUEUE_MAX)
        self.assertEqual(queue[0]["measurement"]["i"], 5)  # oldest dropped
        self.assertGreaterEqual(
            telemetry.counters().get("upload_queue_overflow", 0), 1)

    def test_flush_is_time_boxed_per_cycle(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        for i in range(60):
            comm._enqueue({"measurement": {"i": i}})
        self.fake.clear()
        comm._flush_queue(max_items=25)
        self.assertEqual(len(self.fake.paths("/measurements")), 25)
        self.assertEqual(len(comm._load_queue()), 35)  # rest kept for next cycle

    def test_flush_stops_at_first_failure_and_keeps_order(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        for i in range(5):
            comm._enqueue({"measurement": {"i": i}})
        self.fake.mode = "http500"
        comm._flush_queue()
        remaining = comm._load_queue()
        self.assertEqual([p["measurement"]["i"] for p in remaining], [0, 1, 2, 3, 4])

    def test_queue_save_is_atomic(self):
        comm = self._comm()
        comm._enqueue({"measurement": {"i": 1}})
        # The temp file must not linger, and the target must be valid JSON.
        self.assertFalse(os.path.exists(os.path.join("data", "cloud_upload_queue.json.tmp")))
        json.loads(pathlib.Path("data", "cloud_upload_queue.json").read_text())

    # ── Incident forwarding ────────────────────────────────────────────────────

    def test_submit_incident_posts_structured_event(self):
        comm = self._comm()
        self.assertTrue(comm._ensure_registered())
        self.fake.clear()
        comm.submit_incident({"event": "slew_failed", "severity": "error",
                              "target": "T CrB", "detail": {"timeout_s": 180}})
        self.assertEqual(len(self.fake.paths("/incidents")), 1)
        req = [r for r in self.fake.requests if r["path"].endswith("/incidents")][0]
        self.assertEqual(req["body"]["incident_type"], "slew_failed")
        self.assertEqual(req["body"]["severity"], "error")

    def test_submit_incident_skipped_when_unregistered(self):
        self.fake.mode = "down"
        comm = self._comm()
        comm._register_next_attempt = float("inf")
        self.fake.clear()
        comm.submit_incident({"event": "x", "severity": "error"})  # no raise
        self.assertEqual(self.fake.paths("/incidents"), [])


if __name__ == "__main__":
    unittest.main()
