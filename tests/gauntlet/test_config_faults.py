"""Gauntlet: config.yaml corruption and patching (F6, F13, F20).

The config file has multiple writers (cloud patches, pairing, connect route)
and one nontechnical owner with a text editor.  Corruption must never kill a
watchdog thread, and every write must be atomic.
"""

import os
import pathlib
import unittest

import yaml

from src import telemetry
from src.config_patch import apply_config_patch
from tests.gauntlet.util import TempCwdTestCase

import src.dashboard as dashboard


class ConfigPatchTest(TempCwdTestCase):
    def test_patch_deep_merges_and_preserves_unrelated_keys(self):
        self.write("config.yaml",
                   "cloud:\n  enabled: true\n  url: https://x\nsafety:\n  enabled: true\n")
        apply_config_patch({"cloud": {"activation_code": "BS-2026-NEW"}})
        cfg = yaml.safe_load(pathlib.Path("config.yaml").read_text())
        self.assertEqual(cfg["cloud"]["activation_code"], "BS-2026-NEW")
        self.assertEqual(cfg["cloud"]["url"], "https://x")
        self.assertTrue(cfg["safety"]["enabled"])

    def test_patch_write_is_atomic(self):
        self.write("config.yaml", "cloud:\n  enabled: true\n")
        apply_config_patch({"cloud": {"enabled": False}})
        self.assertFalse(os.path.exists("config.yaml.tmp"))
        self.assertFalse(os.path.exists("config.tmp"))

    def test_patch_against_missing_config_raises(self):
        with self.assertRaises(FileNotFoundError):
            apply_config_patch({"cloud": {"enabled": True}})

    def test_patch_against_non_mapping_config_raises(self):
        self.write("config.yaml", "- just\n- a\n- list\n")
        with self.assertRaises(ValueError):
            apply_config_patch({"cloud": {"enabled": True}})


class LoadConfigResilienceTest(TempCwdTestCase):
    """F6 — a corrupt config must degrade to the last good one, with evidence."""

    GOOD = ("observatory:\n  latitude: 31.0\n  longitude: -99.0\n"
            "cloud:\n  enabled: true\n")

    def setUp(self):
        super().setUp()
        telemetry.reset_for_tests()
        dashboard._last_good_config = {}
        dashboard._config_parse_error_reported = False

    def tearDown(self):
        dashboard._last_good_config = {}
        dashboard._config_parse_error_reported = False
        telemetry.reset_for_tests()
        super().tearDown()

    def test_corrupt_config_falls_back_to_last_good(self):
        self.write("config.yaml", self.GOOD)
        cfg = dashboard._load_config()
        self.assertTrue(cfg["cloud"]["enabled"])

        # Simulate a crash mid-write / fat-fingered edit.
        self.write("config.yaml", "cloud: {enabled: true\n  broken: [unclosed\n")
        cfg = dashboard._load_config()
        self.assertTrue(cfg["cloud"]["enabled"])  # last good survives
        self.assertEqual(telemetry.counters().get("config_parse_failed"), 1)

        # And the error is reported once, not every 30 seconds forever.
        dashboard._load_config()
        self.assertEqual(telemetry.counters().get("config_parse_failed"), 1)

    def test_recovery_is_also_evidenced(self):
        self.write("config.yaml", self.GOOD)
        dashboard._load_config()
        self.write("config.yaml", "]]]broken")
        dashboard._load_config()
        self.write("config.yaml", self.GOOD)
        dashboard._load_config()
        self.assertEqual(telemetry.counters().get("config_parse_recovered"), 1)

    def test_corrupt_config_cannot_kill_the_disconnect_watchdog(self):
        self.write("config.yaml", "{{{{totally broken")
        # The tick must not raise even with no last-good config and no cloud.
        orig_cloud = dashboard._cloud
        dashboard._cloud = None
        try:
            dashboard._cloud_disconnect_tick()
        finally:
            dashboard._cloud = orig_cloud

    def test_non_mapping_root_treated_as_corrupt(self):
        self.write("config.yaml", self.GOOD)
        dashboard._load_config()
        self.write("config.yaml", "- a\n- list\n")
        cfg = dashboard._load_config()
        self.assertTrue(cfg["cloud"]["enabled"])


if __name__ == "__main__":
    unittest.main()
