"""Observatory location must end up everywhere that reads it.

Two independent readers: cloud registration uses `observatory.latitude`,
airmass and the safety horizon use `safety.observer.latitude`. A location
present in only one of them used to leave the other at 0/0 — and a node
configured solely under `safety.observer` skipped auto-detection (it looked
configured) and then registered as 0/0, which the cloud rejects as
"latitude/longitude not set" on every retry, forever.
"""

import unittest
from unittest.mock import patch

from src.geolocation import enrich_config_with_location


def _no_detection():
    return patch("src.geolocation.detect_location", return_value=None)


class LocationMirroringTest(unittest.TestCase):
    def test_safety_only_location_is_mirrored_to_observatory(self):
        cfg = {"observatory": {"latitude": None, "longitude": None},
               "safety": {"observer": {"latitude": 31.5, "longitude": -99.2}}}
        with _no_detection():
            out = enrich_config_with_location(cfg)
        self.assertEqual(out["observatory"]["latitude"], 31.5,
                         "cloud registration would have sent 0.0 and been "
                         "rejected as 'latitude/longitude not set'")
        self.assertEqual(out["observatory"]["longitude"], -99.2)

    def test_observatory_only_location_is_mirrored_to_safety(self):
        cfg = {"observatory": {"latitude": -33.9, "longitude": 18.4},
               "safety": {"observer": {"latitude": 0.0, "longitude": 0.0}}}
        with _no_detection():
            out = enrich_config_with_location(cfg)
        self.assertEqual(out["safety"]["observer"]["latitude"], -33.9,
                         "airmass and horizon checks would use 0/0")
        self.assertEqual(out["safety"]["observer"]["longitude"], 18.4)

    def test_configured_location_is_never_overwritten_by_detection(self):
        cfg = {"observatory": {"latitude": 51.5, "longitude": -0.12},
               "safety": {"observer": {"latitude": 51.5, "longitude": -0.12}}}
        with patch("src.geolocation.detect_location",
                   return_value={"latitude": 1.0, "longitude": 2.0}) as det:
            out = enrich_config_with_location(cfg)
        det.assert_not_called()
        self.assertEqual(out["observatory"]["latitude"], 51.5)

    def test_detected_location_fills_both(self):
        cfg = {"observatory": {"latitude": None, "longitude": None}}
        with patch("src.geolocation.detect_location",
                   return_value={"latitude": 40.0, "longitude": -105.0}):
            out = enrich_config_with_location(cfg)
        self.assertEqual(out["observatory"]["latitude"], 40.0)
        self.assertEqual(out["safety"]["observer"]["longitude"], -105.0)

    def test_no_location_anywhere_stays_unset(self):
        cfg = {"observatory": {"latitude": None, "longitude": None}}
        with _no_detection():
            out = enrich_config_with_location(cfg)
        self.assertIsNone(out["observatory"]["latitude"],
                          "must not invent a location when detection fails")

    def test_missing_sections_are_created(self):
        with _no_detection():
            out = enrich_config_with_location({"safety": {"observer": {
                "latitude": 10.0, "longitude": 20.0}}})
        self.assertEqual(out["observatory"]["latitude"], 10.0)


if __name__ == "__main__":
    unittest.main()
