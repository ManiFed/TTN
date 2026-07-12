#!/usr/bin/env python3
"""
Universal frame ingestion Phase 1 — the staged ingestion worker.

Against a throwaway local PostgreSQL, with the plate solver and the survey
photometry pipeline mocked (the plan's P1 contract: solver mocked in unit
tests, a real solve left to a marked slow test):

  * a WCS-present contribution skips solving and lands its sources,
  * a no-WCS contribution is plate-solved (solver.solve) before extraction,
  * a solve failure marks the row failed at stage 'solve',
  * SKIP LOCKED claiming hands each pending row to exactly one worker.

Run with:  python3 -m unittest tests.test_ingest_worker
"""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cloud import ingest_worker, survey

TEST_DB_NAME = "boundless_ingest_test"
ADMIN_URL = "postgresql://boundless@/boundless?host=/tmp"
TEST_URL = f"postgresql://boundless@/{TEST_DB_NAME}?host=/tmp"

CONFIG = {
    "survey": {"max_sources_per_batch": 800, "retention_days": 120,
               "deviants_per_node_night": 50, "bootstrap_min_delta_mag": 1.0,
               "bootstrap_min_sigma": 5.0, "baseline_min_n": 5,
               "baseline_min_z": 5.0, "baseline_min_delta_mag": 0.3,
               "new_source_min_snr": 10.0, "variable_suspect_stdev": 0.2},
    "solver": {"solve_field_path": "solve-field"},
    # This suite exercises the solve/extract/ingest staging with a stub image;
    # triage (real-pixel heuristics) is covered separately in test_triage.
    "triage": {"enabled": False},
}


def _postgres_available() -> bool:
    try:
        import psycopg2
        psycopg2.connect(ADMIN_URL).close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_postgres_available(), "local postgres not reachable")
class IngestWorkerTest(unittest.TestCase):
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
        for t in ("contributions", "survey_measurements", "survey_sources",
                  "users"):
            self.db.execute(f"DELETE FROM {t}")
        self.db.execute(
            "INSERT INTO users (user_id, email, password_hash, salt, created_at) "
            "VALUES ('u1','a@b.c','h','s',%s)", (self._now(),))
        # A stored file path the worker checks exists — contents don't matter
        # because run_survey_pipeline is mocked.
        self.tmp = Path(self.id().split(".")[-1] + ".fits")
        self.tmp.write_bytes(b"SIMPLE  = T" + b" " * 3000)
        # Patch the pipeline + solver.
        self._orig_pipe = None
        import src.photometry as photo
        self._photo = photo
        self._orig_pipe = getattr(photo, "run_survey_pipeline", None)
        photo.run_survey_pipeline = lambda path, cfg: {
            "bjd": 2460500.5, "filter": "CV", "zp_scatter": 0.03,
            "survey_sources": [{"key": "CAT-1", "ra": 180.0, "dec": 45.0,
                                "mag": 13.0, "mag_err": 0.05, "snr": 40.0,
                                "cat_mag": 13.0, "cat_err": 0.03,
                                "cat_src": "test", "matched": True}],
        }

    def tearDown(self):
        if self._orig_pipe is not None:
            self._photo.run_survey_pipeline = self._orig_pipe
        try:
            self.tmp.unlink()
        except OSError:
            pass

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def _contrib(self, wcs_present=1):
        return self.db.execute(
            "INSERT INTO contributions (user_id, node_id, filename, sha256, "
            " size_bytes, status, wcs_present, stored_path, created_at) "
            "VALUES ('u1','contrib_u1','f.fits',%s,3011,'pending',%s,%s,%s)",
            (f"sha_{wcs_present}_{datetime.now().timestamp()}", wcs_present,
             str(self.tmp), self._now()), returning_id=True)

    def test_wcs_present_skips_solve_and_ingests(self):
        cid = self._contrib(wcs_present=1)
        n = ingest_worker.process_pending(CONFIG)
        self.assertEqual(n, 1)
        row = self.db.query_one("SELECT * FROM contributions WHERE id = %s", (cid,))
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["stage"], "ingest")
        self.assertEqual(row["solved"], 0)          # never solved
        self.assertGreaterEqual(row["n_sources"], 1)
        self.assertIsNotNone(self.db.query_one(
            "SELECT 1 FROM survey_sources WHERE source_key = 'CAT-1'"))

    def test_no_wcs_frame_is_solved_then_ingested(self):
        calls = {}
        from cloud import solver
        orig = solver.solve

        def _fake_solve(path, config, **kw):
            calls["solved"] = True
            return {"ra_deg": 180.0, "dec_deg": 45.0,
                    "pixel_scale_arcsec": 2.4, "wcs_written": True}
        solver.solve = _fake_solve
        try:
            cid = self._contrib(wcs_present=0)
            ingest_worker.process_pending(CONFIG)
        finally:
            solver.solve = orig
        self.assertTrue(calls.get("solved"))
        row = self.db.query_one("SELECT * FROM contributions WHERE id = %s", (cid,))
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["solved"], 1)
        self.assertAlmostEqual(row["pixel_scale_arcsec"], 2.4, places=3)

    def test_solve_failure_marks_failed_at_solve_stage(self):
        from cloud import solver
        orig = solver.solve
        solver.solve = lambda path, config, **kw: None   # solver can't solve it
        try:
            cid = self._contrib(wcs_present=0)
            ingest_worker.process_pending(CONFIG)
        finally:
            solver.solve = orig
        row = self.db.query_one("SELECT * FROM contributions WHERE id = %s", (cid,))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["stage"], "solve")
        self.assertIn("solve", row["error"])

    def test_claim_one_is_exclusive(self):
        cid = self._contrib(wcs_present=1)
        first = ingest_worker._claim_one()
        self.assertEqual(first["id"], cid)
        # Already claimed → a second claim finds nothing pending.
        self.assertIsNone(ingest_worker._claim_one())


if __name__ == "__main__":
    unittest.main()
