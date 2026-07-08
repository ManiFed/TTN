#!/usr/bin/env python3
"""
Open Aperture cloud-side tests: deviation statistics and the discovery
candidate lifecycle.

Two layers:

    DeviationThresholdsTest — the two-tier statistics as pure logic,
        with detection recording stubbed out.

    SurveyLifecycleIntegrationTest — the real thing against a throwaway
        local PostgreSQL database: SQL Welford aggregates, measurement
        dedup, the 2-detection + corroboration gates, promotion to
        crossmatching, human confirmation into the targets table CHORUS
        schedules from, and retention pruning. Skipped when no local
        postgres is reachable.

Run with:  python3 -m unittest tests.test_survey_cloud
"""

import json
import unittest
from datetime import datetime, timezone

import numpy as np

from cloud import survey

TEST_DB_NAME = "boundless_oa_test"
ADMIN_URL = "postgresql://boundless@/boundless?host=/tmp"
TEST_URL = f"postgresql://boundless@/{TEST_DB_NAME}?host=/tmp"

CONFIG = {
    "survey": {
        "max_sources_per_batch": 800,
        "retention_days": 120,
        "deviants_per_node_night": 50,
        "bootstrap_min_delta_mag": 1.0,
        "bootstrap_min_sigma": 5.0,
        "baseline_min_n": 5,
        "baseline_min_z": 5.0,
        "baseline_min_delta_mag": 0.3,
        "new_source_min_snr": 10.0,
        "variable_suspect_stdev": 0.2,
    }
}


def _postgres_available() -> bool:
    try:
        import psycopg2
        psycopg2.connect(ADMIN_URL).close()
        return True
    except Exception:
        return False


def _src(key="CAT-001", mag=13.0, mag_err=0.05, snr=40.0, cat_mag=13.0,
         cat_err=0.03, matched=True, ra=180.0, dec=45.0):
    return {"key": key, "ra": ra, "dec": dec, "mag": mag, "mag_err": mag_err,
            "snr": snr, "cat_mag": cat_mag if matched else None,
            "cat_err": cat_err if matched else None,
            "cat_src": "test" if matched else "", "matched": matched}


class DeviationThresholdsTest(unittest.TestCase):
    """The two-tier statistics, with _record_detection stubbed to capture."""

    def setUp(self):
        self.captured = []
        self._orig = survey._record_detection
        survey._record_detection = (
            lambda node_id, bjd, filt, s, kind, delta, config, tier=1:
                self.captured.append((s["key"], kind, delta)) or True)

    def tearDown(self):
        survey._record_detection = self._orig

    def _run(self, sources, baselines=None, zp_scatter=0.03):
        self.captured.clear()
        return survey._detect_deviations(
            "node_t1", 2460500.5, "CV", zp_scatter, sources,
            baselines or {}, CONFIG)

    def test_bootstrap_brightening_fires(self):
        self._run([_src(mag=11.4, cat_mag=13.0)])
        self.assertEqual([c[1] for c in self.captured], ["brightening"])
        self.assertAlmostEqual(self.captured[0][2], 1.6, places=3)

    def test_bootstrap_small_delta_ignored(self):
        self._run([_src(mag=12.2, cat_mag=13.0)])   # Δ=0.8 < 1.0
        self.assertEqual(self.captured, [])

    def test_bootstrap_never_flags_fading(self):
        self._run([_src(mag=15.0, cat_mag=13.0)])   # fainter than catalog
        self.assertEqual(self.captured, [])

    def test_bootstrap_respects_sigma_floor(self):
        # Δ=1.2 passes the mag bar but not 5σ with huge errors.
        self._run([_src(mag=11.8, cat_mag=13.0, mag_err=0.4, cat_err=0.3)])
        self.assertEqual(self.captured, [])

    def _baseline(self, n=10, mean=13.0, sd=0.02):
        return {"CAT-001": {"n_obs": n, "mean_mag": mean,
                            "m2": (n - 1) * sd ** 2,
                            "vsx_name": "", "variability_flag": "none"}}

    def test_baselined_fading_fires(self):
        self._run([_src(mag=13.5)], self._baseline())
        self.assertEqual([c[1] for c in self.captured], ["fading"])

    def test_baselined_brightening_fires(self):
        self._run([_src(mag=12.5)], self._baseline())
        self.assertEqual([c[1] for c in self.captured], ["brightening"])

    def test_baselined_small_delta_ignored(self):
        self._run([_src(mag=13.2)], self._baseline())   # |Δ|=0.2 < 0.3
        self.assertEqual(self.captured, [])

    def test_baselined_noisy_history_raises_bar(self):
        # Own scatter 0.15: |Δ|=0.5 is only ~3.2σ — below the z≥5 gate.
        self._run([_src(mag=13.5)], self._baseline(sd=0.15))
        self.assertEqual(self.captured, [])

    def test_vsx_known_suppressed(self):
        base = self._baseline()
        base["CAT-001"]["vsx_name"] = "SS Cyg"
        self._run([_src(mag=11.0)], base)
        self.assertEqual(self.captured, [])

    def test_variable_suspect_suppressed(self):
        base = self._baseline()
        base["CAT-001"]["variability_flag"] = "variable_suspect"
        self._run([_src(mag=11.0)], base)
        self.assertEqual(self.captured, [])

    def test_new_source_needs_snr(self):
        self._run([_src(key="p180.1+45.0", matched=False, snr=8.0)])
        self.assertEqual(self.captured, [])
        self._run([_src(key="p180.1+45.0", matched=False, snr=15.0)])
        self.assertEqual([c[1] for c in self.captured], ["new_source"])


@unittest.skipUnless(_postgres_available(), "local postgres not reachable")
class SurveyLifecycleIntegrationTest(unittest.TestCase):
    """detected → crossmatching → candidate → confirmed → targets,
    against a real (throwaway) PostgreSQL database."""

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
        for table in ("survey_measurements", "survey_sources",
                      "discovery_candidates"):
            self.db.execute(f"DELETE FROM {table}")

    def _batch(self, node="node_a", bjd=2460500.500, sources=None,
               tier=1, zp_scatter=0.03):
        return survey.ingest_batch(
            node,
            {"frame": {"bjd": bjd, "filter": "CV", "zp_scatter": zp_scatter,
                       "fits_file": f"t_{bjd:.3f}.fits"},
             "sources": sources or [_src()]},
            CONFIG, node_tier=tier)

    # ── Welford aggregates in SQL ──────────────────────────────────────────────

    def test_welford_matches_numpy(self):
        mags = [13.00, 13.05, 12.95, 13.10, 12.90, 13.02]
        for i, m in enumerate(mags):
            r = self._batch(bjd=2460500.5 + i * 0.01,
                            sources=[_src(mag=m)])
            self.assertTrue(r["ok"], r)
        row = self.db.query_one(
            "SELECT n_obs, mean_mag, m2 FROM survey_sources "
            "WHERE source_key = 'CAT-001'")
        self.assertEqual(row["n_obs"], len(mags))
        self.assertAlmostEqual(row["mean_mag"], float(np.mean(mags)), places=9)
        self.assertAlmostEqual(row["m2"],
                               float(np.var(mags, ddof=1)) * (len(mags) - 1),
                               places=9)

    def test_duplicate_frame_never_double_counts(self):
        first = self._batch()
        self.assertEqual(first["accepted"], 1)
        replay = self._batch()   # same node, same bjd — a retried upload
        self.assertEqual(replay["accepted"], 0)
        self.assertEqual(replay["duplicates"], 1)
        row = self.db.query_one(
            "SELECT n_obs FROM survey_sources WHERE source_key = 'CAT-001'")
        self.assertEqual(row["n_obs"], 1, "Welford must ignore replays")

    def test_batch_bounds_rejected(self):
        r = survey.ingest_batch("node_a", {"frame": {"bjd": "nope"},
                                           "sources": [_src()]}, CONFIG)
        self.assertFalse(r["ok"])
        r = self._batch(sources=[_src(mag=99.0)])   # out of bounds
        self.assertFalse(r["ok"])

    # ── Candidate lifecycle ────────────────────────────────────────────────────

    def test_transient_walks_to_crossmatching_and_confirmed(self):
        bright = _src(key="CAT-NOVA", mag=11.0, cat_mag=13.0, ra=181.5,
                      dec=44.0)
        self._batch(node="node_a", bjd=2460500.500, sources=[bright])
        cand = self.db.query_one(
            "SELECT * FROM discovery_candidates WHERE source_key = 'CAT-NOVA'")
        self.assertIsNotNone(cand)
        self.assertEqual(cand["state"], "detected")

        # Co-temporal corroboration from a second scheduled node.
        self._batch(node="node_b", bjd=2460500.510, sources=[bright])
        cand = self.db.query_one(
            "SELECT * FROM discovery_candidates WHERE source_key = 'CAT-NOVA'")
        self.assertEqual(cand["state"], "crossmatching")
        self.assertEqual(cand["n_nodes"], 2)
        self.assertEqual(cand["n_detections"], 2)

        # Human confirms (crossmatch found nothing — simulate by moving to
        # candidate first, as the worker would).
        self.db.execute("UPDATE discovery_candidates SET state='candidate' "
                        "WHERE id = %s", (cand["id"],))
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO users (user_id, email, password_hash, salt, created_at)"
            " VALUES ('u_test','t@bs.org','x','y',%s)", (now,))
        self.db.execute(
            "INSERT INTO node_members (node_id, user_id, claimed_at)"
            " VALUES ('node_a','u_test',%s)", (now,))

        from cloud import crossmatch
        result = crossmatch.confirm_candidate(cand["id"], target_type="NOVA")
        self.assertIsNotNone(result)
        target = self.db.query_one(
            "SELECT * FROM targets WHERE target_id = %s",
            (result["target_id"],))
        self.assertIsNotNone(target, "confirmed discovery must become a "
                                     "target CHORUS can schedule")
        self.assertEqual(target["active"], 1)
        self.assertEqual(target["time_critical"], 1)
        self.assertIn("open_aperture", target["sources"])
        highlight = self.db.query_one(
            "SELECT * FROM member_highlights WHERE user_id = 'u_test'")
        self.assertIsNotNone(highlight, "detecting node's owner gets credit")

    def test_contributor_alone_cannot_promote(self):
        bright = _src(key="CAT-POISON", mag=11.0, cat_mag=13.0)
        self._batch(node="node_c1", bjd=2460500.500, sources=[bright], tier=0)
        self._batch(node="node_c1", bjd=2460500.510, sources=[bright], tier=0)
        cand = self.db.query_one(
            "SELECT * FROM discovery_candidates WHERE source_key = 'CAT-POISON'")
        self.assertEqual(cand["state"], "detected",
                         "a lone contributor must never promote a candidate")
        # A second, independent contributor corroborates.
        self._batch(node="node_c2", bjd=2460500.512, sources=[bright], tier=0)
        cand = self.db.query_one(
            "SELECT * FROM discovery_candidates WHERE source_key = 'CAT-POISON'")
        self.assertEqual(cand["state"], "crossmatching")

    def test_deviant_flood_cap(self):
        cfg = {"survey": dict(CONFIG["survey"], deviants_per_node_night=3)}
        for i in range(6):
            s = _src(key=f"CAT-F{i:02d}", mag=11.0, cat_mag=13.0,
                     ra=180.0 + i, dec=45.0)
            survey.ingest_batch(
                "node_flood",
                {"frame": {"bjd": 2460500.5 + i * 0.01, "filter": "CV",
                           "zp_scatter": 0.03, "fits_file": f"f{i}.fits"},
                 "sources": [s]}, cfg)
        n = self.db.query_one(
            "SELECT COUNT(*) AS n FROM discovery_candidates "
            "WHERE source_key LIKE %s", ("CAT-F%",))["n"]
        self.assertEqual(n, 3, "per-node nightly deviant cap must hold")

    def test_noisy_star_becomes_variable_suspect_not_alerts(self):
        # A star swinging ±0.5 mag builds a noisy baseline; once flagged it
        # stops alerting and becomes survey science.
        for i in range(12):
            mag = 13.0 + (0.5 if i % 2 else -0.5)
            self._batch(node="node_v", bjd=2460500.5 + i * 0.02,
                        sources=[_src(key="CAT-VAR", mag=mag, cat_mag=13.0)])
        row = self.db.query_one(
            "SELECT variability_flag FROM survey_sources "
            "WHERE source_key = 'CAT-VAR'")
        self.assertEqual(row["variability_flag"], "variable_suspect")
        n_before = self.db.query_one(
            "SELECT COUNT(*) AS n FROM discovery_candidates "
            "WHERE source_key = 'CAT-VAR'")["n"]
        self._batch(node="node_v", bjd=2460501.9,
                    sources=[_src(key="CAT-VAR", mag=11.0, cat_mag=13.0)])
        n_after = self.db.query_one(
            "SELECT COUNT(*) AS n FROM discovery_candidates "
            "WHERE source_key = 'CAT-VAR'")["n"]
        self.assertEqual(n_before, n_after,
                         "flagged variables must stop opening candidates")

    def test_retention_prunes_raw_but_keeps_aggregates(self):
        self._batch()
        self.db.execute(
            "UPDATE survey_measurements SET received_at = '2020-01-01T00:00:00'")
        deleted = survey.prune_survey_measurements(CONFIG)
        self.assertEqual(deleted, 1)
        row = self.db.query_one(
            "SELECT n_obs FROM survey_sources WHERE source_key = 'CAT-001'")
        self.assertEqual(row["n_obs"], 1,
                         "aggregates are the permanent science product")


if __name__ == "__main__":
    unittest.main()
