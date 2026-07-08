"""Gauntlet: Companion (contributor mode) vs. a fake cloud.

Asserts that a watched FITS is uploaded to the member contributions endpoint,
that the same frame is never re-sent (local sha dedup), and that a server-side
duplicate (409) is treated as success.
"""

import unittest
from pathlib import Path

from src.companion import Companion
from tests.gauntlet.fakecloud import FakeCloud
from tests.gauntlet.util import TempCwdTestCase


def _config(url: str) -> dict:
    return {"contributor": {"enabled": True, "cloud_url": url,
                            "member_token": "tok123",
                            "watch_dirs": ["lights"], "scan_existing": False}}


class CompanionTest(TempCwdTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeCloud().start()
        Path("lights").mkdir()
        self.frame = Path("lights") / "m31.fits"
        self.frame.write_bytes(b"SIMPLE  = T" + b" " * 3000)

    def tearDown(self):
        self.fake.stop()
        super().tearDown()

    def test_uploads_a_frame(self):
        comp = Companion(_config(self.fake.url))
        r = comp.upload_file(str(self.frame))
        self.assertTrue(r["ok"], r)
        self.assertEqual(comp.stats["uploaded"], 1)
        self.assertIn("/api/v1/me/contributions", self.fake.paths())

    def test_dedupes_same_frame(self):
        comp = Companion(_config(self.fake.url))
        comp.upload_file(str(self.frame))
        self.fake.clear()
        r2 = comp.upload_file(str(self.frame))     # identical bytes
        self.assertTrue(r2.get("skipped"))
        self.assertEqual(self.fake.paths("/contributions"), [])   # no 2nd POST
        self.assertEqual(comp.stats["skipped"], 1)

    def test_server_duplicate_409_is_success(self):
        self.fake.contrib_status = 409
        comp = Companion(_config(self.fake.url))
        r = comp.upload_file(str(self.frame))
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("duplicate"))

    def test_dedup_survives_restart(self):
        comp = Companion(_config(self.fake.url))
        comp.upload_file(str(self.frame))
        # A fresh Companion (simulated restart) reads the uploaded-sha state.
        comp2 = Companion(_config(self.fake.url))
        self.fake.clear()
        r = comp2.upload_file(str(self.frame))
        self.assertTrue(r.get("skipped"))
        self.assertEqual(self.fake.paths("/contributions"), [])


if __name__ == "__main__":
    unittest.main()
