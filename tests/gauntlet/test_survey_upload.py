"""Gauntlet: survey upload queue vs. an unreliable cloud.

Survey source lists ride their own byte-capped disk queue, separate from the
measurement queue. These tests assert: gzip delivery works, failures queue to
disk and survive restarts, recovery flushes in order, and the byte cap drops
oldest-first with telemetry evidence.
"""

import json
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
        },
        "observatory": {"latitude": 31.36, "longitude": -99.44,
                        "elevation": 500, "observer": "Gauntlet"},
        "photometry": {"node_id": "", "filter_name": "CV"},
    }


def _frame(n: int) -> dict:
    return {"bjd": 2460500.5 + n * 0.01, "filter": "CV",
            "zero_point": 22.0, "zp_scatter": 0.03,
            "fits_file": f"stack_{n:03d}.fits"}


def _sources(n_stars: int = 20) -> list:
    return [{"key": f"CAT-{i:03d}", "ra": 180.0 + i * 0.01, "dec": 45.0,
             "mag": 12.0 + i * 0.1, "mag_err": 0.05, "snr": 40.0,
             "cat_mag": 12.0 + i * 0.1, "cat_err": 0.03,
             "cat_src": "test", "matched": True} for i in range(n_stars)]


class SurveyUploadGauntletTest(TempCwdTestCase):
    def setUp(self):
        super().setUp()
        telemetry.reset_for_tests()
        self.fake = FakeCloud().start()

    def tearDown(self):
        self.fake.stop()
        telemetry.reset_for_tests()
        super().tearDown()

    def _comm(self) -> CloudCommunicator:
        comm = CloudCommunicator(_config(self.fake.url))
        self.assertTrue(comm._ensure_registered())
        return comm

    def test_survey_upload_delivers_gzipped_payload(self):
        comm = self._comm()
        self.assertTrue(comm.submit_survey(_frame(1), _sources()))
        bodies = [r["body"] for r in self.fake.requests
                  if r["path"] == "/api/v1/survey"]
        self.assertEqual(len(bodies), 1)
        # FakeCloud gunzips on receipt — a parsed body proves the encoding.
        self.assertEqual(bodies[0]["frame"]["fits_file"], "stack_001.fits")
        self.assertEqual(len(bodies[0]["sources"]), 20)

    def test_failure_queues_to_disk_and_recovery_flushes(self):
        comm = self._comm()
        self.fake.mode = "http500"
        self.assertFalse(comm.submit_survey(_frame(1), _sources()))
        self.assertFalse(comm.submit_survey(_frame(2), _sources()))
        queue_file = pathlib.Path("data", "survey_upload_queue.json")
        self.assertTrue(queue_file.exists())
        self.assertEqual(len(json.loads(queue_file.read_text())), 2)
        self.assertEqual(comm.status["queued_survey_uploads"], 2)

        self.fake.mode = "ok"
        self.fake.clear()
        comm._flush_survey_queue()
        self.assertEqual(comm.status["queued_survey_uploads"], 0)
        sent = [r["body"]["frame"]["fits_file"] for r in self.fake.requests
                if r["path"] == "/api/v1/survey"]
        self.assertEqual(sent, ["stack_001.fits", "stack_002.fits"],
                         "flush must preserve capture order")

    def test_queue_survives_restart(self):
        comm = self._comm()
        self.fake.mode = "down"
        comm.submit_survey(_frame(7), _sources())
        # Simulated restart: a fresh communicator sees the queued frame.
        comm2 = CloudCommunicator(_config(self.fake.url))
        self.assertEqual(comm2.status["queued_survey_uploads"], 1)
        self.fake.mode = "ok"
        self.fake.clear()
        self.assertTrue(comm2._ensure_registered())
        comm2._flush_survey_queue()
        sent = [r for r in self.fake.requests if r["path"] == "/api/v1/survey"]
        self.assertEqual(len(sent), 1)

    def test_byte_cap_drops_oldest_frames_with_evidence(self):
        import src.cloud_communicator as cc
        comm = self._comm()
        self.fake.mode = "http500"
        orig_cap = cc._SURVEY_QUEUE_MAX_BYTES
        cc._SURVEY_QUEUE_MAX_BYTES = 20_000   # a few frames' worth
        try:
            for n in range(10):
                comm.submit_survey(_frame(n), _sources(30))
            queue = json.loads(pathlib.Path(
                "data", "survey_upload_queue.json").read_text())
            self.assertLess(len(queue), 10, "cap must have dropped frames")
            # Newest frames survive; oldest were sacrificed.
            kept = [q["frame"]["fits_file"] for q in queue]
            self.assertIn("stack_009.fits", kept)
            self.assertNotIn("stack_000.fits", kept)
            self.assertGreaterEqual(
                telemetry.counters().get("survey_queue_overflow", 0), 1,
                "overflow must leave structured evidence")
        finally:
            cc._SURVEY_QUEUE_MAX_BYTES = orig_cap

    def test_flush_stops_at_first_failure(self):
        comm = self._comm()
        self.fake.mode = "http500"
        for n in range(3):
            comm.submit_survey(_frame(n), _sources())
        self.assertEqual(comm.status["queued_survey_uploads"], 3)
        # Cloud still down: flush attempts once, keeps everything.
        self.fake.clear()
        comm._flush_survey_queue()
        self.assertEqual(comm.status["queued_survey_uploads"], 3)
        attempts = [r for r in self.fake.requests
                    if r["path"] == "/api/v1/survey"]
        self.assertEqual(len(attempts), 1, "no hammering on a dead cloud")


if __name__ == "__main__":
    unittest.main()
