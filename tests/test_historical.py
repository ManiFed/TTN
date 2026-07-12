#!/usr/bin/env python3
"""
Universal frame ingestion Phase 4 — historical ingestion + retrospective discovery.

Against a throwaway local PostgreSQL:

  * Welford baselines are order-independent — ingesting the same magnitudes in
    forward vs shuffled order yields identical n_obs / mean / m2,
  * a historical deviation lands in retro_discoveries and NEVER opens a live
    discovery_candidate or fires an interrupt,
  * a historical deviant point is excluded from the baseline fold (so a nova
    can't inflate the mean it's compared against),
  * provenance (contribution_id, user_id) is attached to measurements + retros.

Run with:  python3 -m unittest tests.test_historical
"""

import unittest

from cloud import survey

TEST_DB_NAME = "boundless_hist_test"
ADMIN_URL = "postgresql://boundless@/boundless?host=/tmp"
TEST_URL = f"postgresql://boundless@/{TEST_DB_NAME}?host=/tmp"

CONFIG = {"survey": {"max_sources_per_batch": 800, "retention_days": 120,
                     "deviants_per_node_night": 50, "bootstrap_min_delta_mag": 1.0,
                     "bootstrap_min_sigma": 5.0, "baseline_min_n": 5,
                     "baseline_min_z": 5.0, "baseline_min_delta_mag": 0.3,
                     "new_source_min_snr": 10.0, "variable_suspect_stdev": 0.2}}


def _postgres_available() -> bool:
    try:
        import psycopg2
        psycopg2.connect(ADMIN_URL).close()
        return True
    except Exception:
        return False


def _src(key="CAT-1", mag=13.0, ra=180.0, dec=45.0, cat_mag=13.0, matched=True):
    return {"key": key, "ra": ra, "dec": dec, "mag": mag, "mag_err": 0.03,
            "snr": 60.0, "cat_mag": cat_mag if matched else None,
            "cat_err": 0.03 if matched else None,
            "cat_src": "test" if matched else "", "matched": matched}


@unittest.skipUnless(_postgres_available(), "local postgres not reachable")
class HistoricalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        from cloud import db
        admin = psycopg2.connect(ADMIN_URL)
        admin.autocommit = True
        cur = admin.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
        cur.execute(f"CREATE DATABASE {TEST_DB_NAME}")
        admin.close()
        db.init(TEST_URL)
        cls.db = db

    @classmethod
    def tearDownClass(cls):
        import psycopg2
        if cls.db._pool is not None:
            cls.db._pool.closeall()
            cls.db._pool = None
        admin = psycopg2.connect(ADMIN_URL)
        admin.autocommit = True
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
        admin.close()

    def setUp(self):
        survey._deviant_counts.clear()
        for t in ("survey_measurements", "survey_sources",
                  "discovery_candidates", "retro_discoveries"):
            self.db.execute(f"DELETE FROM {t}")

    def _ingest(self, mag, bjd, node="node_a", historical=False, prov=None,
                key="CAT-1", cat_mag=13.0, matched=True):
        return survey.ingest_batch(
            node,
            {"frame": {"bjd": bjd, "filter": "CV", "zp_scatter": 0.03,
                       "fits_file": f"f_{bjd:.3f}.fits"},
             "sources": [_src(key=key, mag=mag, cat_mag=cat_mag, matched=matched)]},
            CONFIG, node_tier=1, historical=historical, provenance=prov)

    def _baseline(self):
        r = self.db.query_one(
            "SELECT n_obs, mean_mag, m2 FROM survey_sources WHERE source_key='CAT-1'")
        return (r["n_obs"], round(r["mean_mag"], 9), round(r["m2"], 9))

    def test_welford_order_independent(self):
        mags = [13.00, 13.05, 12.95, 13.10, 12.90, 13.02, 12.98]
        for i, m in enumerate(mags):                       # forward
            self._ingest(m, 2460500.0 + i * 0.01, node="fwd")
        fwd = self._baseline()

        self.db.execute("DELETE FROM survey_measurements")
        self.db.execute("DELETE FROM survey_sources")
        shuffled = [13.02, 12.90, 13.10, 12.98, 13.00, 12.95, 13.05]
        for i, m in enumerate(shuffled):                   # shuffled, distinct node
            self._ingest(m, 2460400.0 + i * 0.01, node="shf")
        shf = self._baseline()

        self.assertEqual(fwd[0], shf[0])                   # same n_obs
        self.assertAlmostEqual(fwd[1], shf[1], places=6)   # same mean
        self.assertAlmostEqual(fwd[2], shf[2], places=6)   # same m2

    def test_historical_deviation_goes_to_retro_not_live(self):
        # A big brightening vs catalog, ingested as historical.
        r = self._ingest(11.0, 2459000.0, historical=True,
                         prov={"contribution_id": 7, "user_id": "u1"})
        self.assertTrue(r["ok"])
        self.assertTrue(r["historical"])
        self.assertEqual(r["deviants"], 1)
        # It's in retro_discoveries with provenance...
        retro = self.db.query_one("SELECT * FROM retro_discoveries")
        self.assertIsNotNone(retro)
        self.assertEqual(retro["kind"], "brightening")
        self.assertEqual(retro["contribution_id"], 7)
        self.assertEqual(retro["user_id"], "u1")
        # ...and NOT in the live candidate flow.
        self.assertIsNone(self.db.query_one("SELECT 1 FROM discovery_candidates"))

    def test_historical_deviant_excluded_from_baseline(self):
        # Build a baseline around 13.0, then a historical nova at 9.0.
        for i in range(6):
            self._ingest(13.0, 2460500.0 + i * 0.01)
        base_before = self._baseline()
        r = self._ingest(9.0, 2459000.0, historical=True)   # 4 mag brighter
        self.assertEqual(r["deviants"], 1)
        base_after = self._baseline()
        # The nova point was NOT folded: n_obs and mean are unchanged.
        self.assertEqual(base_after[0], base_before[0])
        self.assertAlmostEqual(base_after[1], base_before[1], places=6)
        # Its raw measurement is still archived, though.
        self.assertIsNotNone(self.db.query_one(
            "SELECT 1 FROM survey_measurements WHERE mag = 9.0"))

    def test_historical_nondeviant_still_builds_baseline(self):
        r = self._ingest(13.01, 2459000.0, historical=True)
        self.assertEqual(r["deviants"], 0)
        self.assertEqual(self._baseline()[0], 1)            # folded normally

    def test_provenance_on_measurements(self):
        self._ingest(13.0, 2460500.0, prov={"contribution_id": 42, "user_id": "uZ"})
        m = self.db.query_one(
            "SELECT contribution_id, user_id FROM survey_measurements")
        self.assertEqual(m["contribution_id"], 42)
        self.assertEqual(m["user_id"], "uZ")


if __name__ == "__main__":
    unittest.main()
