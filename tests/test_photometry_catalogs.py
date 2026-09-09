#!/usr/bin/env python3
"""Comparison-catalog client fixes for VSP / APASS / Gaia (issue #69)."""

import unittest
from unittest.mock import MagicMock, patch

import astropy.units as u
from astropy.coordinates import SkyCoord

import src.photometry as P


class VspClientTest(unittest.TestCase):
    def test_coord_parser_sexagesimal_ra(self):
        # 01:00:00 → 15 deg
        self.assertAlmostEqual(P._vsp_coord_to_deg("01:00:00", is_ra=True), 15.0)

    def test_coord_parser_decimal_degrees(self):
        self.assertAlmostEqual(P._vsp_coord_to_deg("325.67892", is_ra=True), 325.67892)

    def test_star_name_400_falls_back_to_radec(self):
        calls = []

        class Resp:
            def __init__(self, code, payload=None):
                self.status_code = code
                self._payload = payload or {}

            def json(self):
                return self._payload

        def fake_get(url, params=None, timeout=45, **kwargs):
            calls.append(dict(params or {}))
            if "star" in (params or {}):
                return Resp(400)
            return Resp(200, {"photometry": [{
                "auid": "000-AAA-001",
                "ra": "10:00:00",
                "dec": "+20:00:00",
                "bands": [{"band": "V", "mag": 12.1, "error": 0.02}],
            }]})

        with patch.dict("sys.modules", {"requests": MagicMock(get=fake_get)}):
            import requests as req_mod
            with patch.object(req_mod, "get", side_effect=fake_get):
                stars = P._get_comparison_stars_aavso(
                    "KELT-20b", 150.0, 20.0, 0.5, 15.0)
        self.assertEqual(len(calls), 2)
        self.assertIn("star", calls[0])
        self.assertIn("ra", calls[1])
        self.assertEqual(len(stars), 1)
        self.assertEqual(stars[0]["source"], "aavso_V")

    def test_fov_is_clamped(self):
        seen = {}

        class Resp:
            status_code = 200
            def json(self):
                return {"photometry": []}

        def fake_get(url, params=None, timeout=45, **kwargs):
            seen.update(params or {})
            return Resp()

        with patch.dict("sys.modules", {"requests": MagicMock()}):
            import requests as req_mod
            with patch.object(req_mod, "get", side_effect=fake_get):
                P._get_comparison_stars_aavso("", 10.0, 20.0, 5.0, 15.0)
        self.assertEqual(seen.get("fov"), 180)



    def test_empty_or_whitespace_star_skips_star_param(self):
        """Issue #81: never query VSP with star='' (HTTP 400)."""
        for name in ("", "   ", "\t", None):
            calls = []

            class Resp:
                status_code = 200
                def json(self):
                    return {"photometry": []}

            def fake_get(url, params=None, timeout=45, **kwargs):
                calls.append(dict(params or {}))
                return Resp()

            with patch.dict("sys.modules", {"requests": MagicMock()}):
                import requests as req_mod
                with patch.object(req_mod, "get", side_effect=fake_get):
                    P._get_comparison_stars_aavso(name, 10.0, 20.0, 0.5, 15.0)
            self.assertEqual(len(calls), 1, msg=repr(name))
            self.assertNotIn("star", calls[0], msg=repr(name))
            self.assertIn("ra", calls[0])
            self.assertIn("dec", calls[0])

    def test_default_timeout_is_45s(self):
        seen = {}

        class Resp:
            status_code = 200
            def json(self):
                return {"photometry": []}

        def fake_get(url, params=None, timeout=None, **kwargs):
            seen["timeout"] = timeout
            return Resp()

        with patch.dict("sys.modules", {"requests": MagicMock()}):
            import requests as req_mod
            with patch.object(req_mod, "get", side_effect=fake_get):
                P._get_comparison_stars_aavso("SS Cyg", 325.83, 43.59, 0.5, 15.0)
        self.assertEqual(seen.get("timeout"), 45.0)

    def test_star_timeout_still_tries_radec_attempt(self):
        """Per-attempt timeout must not abort the RA/Dec fallback."""
        calls = []

        class Resp:
            status_code = 200
            def json(self):
                return {"photometry": [{
                    "auid": "000-BCP-001",
                    "ra": "21:42:42",
                    "dec": "+43:35:10",
                    "bands": [{"band": "V", "mag": 11.5, "error": 0.02}],
                }]}

        def fake_get(url, params=None, timeout=45, **kwargs):
            calls.append(dict(params or {}))
            if "star" in (params or {}):
                raise TimeoutError("VSP hung on star=")
            return Resp()

        with patch.dict("sys.modules", {"requests": MagicMock()}):
            import requests as req_mod
            with patch.object(req_mod, "get", side_effect=fake_get):
                stars = P._get_comparison_stars_aavso(
                    "SS Cyg", 325.83, 43.59, 0.5, 15.0, timeout_s=45)
        self.assertEqual(len(calls), 2)
        self.assertIn("star", calls[0])
        self.assertIn("ra", calls[1])
        self.assertEqual(len(stars), 1)

    def test_configurable_timeout_passed_through(self):
        seen = {}

        class Resp:
            status_code = 200
            def json(self):
                return {"photometry": []}

        def fake_get(url, params=None, timeout=None, **kwargs):
            seen["timeout"] = timeout
            return Resp()

        with patch.dict("sys.modules", {"requests": MagicMock()}):
            import requests as req_mod
            with patch.object(req_mod, "get", side_effect=fake_get):
                P._get_comparison_stars_aavso(
                    "SS Cyg", 325.83, 43.59, 0.5, 15.0, timeout_s=60)
        self.assertEqual(seen.get("timeout"), 60.0)


class GaiaConeSearchArityTest(unittest.TestCase):
    def test_radius_passed_as_keyword(self):
        calls = []

        class FakeGaia:
            MAIN_GAIA_TABLE = ""
            ROW_LIMIT = 0

            @staticmethod
            def cone_search_async(coordinate, *args, radius=None, **kwargs):
                # Mimic current astroquery: radius is keyword-only.
                if args:
                    raise TypeError(
                        "cone_search_async() takes 2 positional arguments but 3 were given")
                calls.append({"coordinate": coordinate, "radius": radius})

                class Job:
                    def get_results(self):
                        return None
                return Job()

        fake_u = MagicMock()
        # Keep real Quantity behaviour via SkyCoord; only stub Gaia import path.
        with patch.dict("sys.modules", {"astroquery": MagicMock(),
                                        "astroquery.gaia": MagicMock(Gaia=FakeGaia)}):
            # Re-import path used inside the function
            import astroquery.gaia as gaia_mod
            gaia_mod.Gaia = FakeGaia
            stars = P._get_comparison_stars_gaia(280.0, -60.0, 0.1, 15.0, n_max=5)
        self.assertEqual(stars, [])
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0]["radius"])


class ApassVizierServerTest(unittest.TestCase):
    def test_pins_cds_server_and_list_catalog(self):
        pinned = {}

        class FakeConf:
            server = "old.example"

        class FakeVizier:
            def __init__(self, columns=None, column_filters=None):
                self.ROW_LIMIT = 0
                self.TIMEOUT = 0
                self.columns = columns

            def query_region(self, coord, radius=None, catalog=None):
                pinned["catalog"] = catalog
                return []

        with patch.dict("sys.modules", {
            "astroquery": MagicMock(),
            "astroquery.vizier": MagicMock(Vizier=FakeVizier, conf=FakeConf),
        }):
            import astroquery.vizier as viz_mod
            viz_mod.Vizier = FakeVizier
            viz_mod.conf = FakeConf
            stars = P._get_comparison_stars_apass(10.0, 20.0, 0.2, 15.0)
        self.assertEqual(stars, [])
        self.assertEqual(FakeConf.server, "vizier.cds.unistra.fr")
        self.assertEqual(pinned.get("catalog"), ["II/336/apass9"])


if __name__ == "__main__":
    unittest.main()
