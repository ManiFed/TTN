#!/usr/bin/env python3
"""
Asteroid/minor-planet astrometry tests: tracklet-linking geometry as pure
logic, the moving-object detection → tracklet → human-verdict lifecycle
against a real (throwaway) PostgreSQL database, and the MPC ADES PSV report
formatter.

Run with:  python3 -m unittest tests.test_moving_objects
"""

import re
import unittest
from datetime import datetime, timedelta, timezone

from cloud import moving_objects, mpc_report

TEST_DB_NAME = "boundless_mpc_test"
ADMIN_URL = "postgresql://boundless@/boundless?host=/tmp"
TEST_URL = f"postgresql://boundless@/{TEST_DB_NAME}?host=/tmp"

CONFIG = {
    "mpc": {
        "min_detection_snr": 5.0,
        "link_window_hours": 6.0,
        "link_max_field_sep_deg": 2.0,
        "min_track_detections": 3,
        "max_fit_residual_arcsec": 5.0,
        "arc_link_window_days": 14.0,
        "arc_max_pred_resid_arcsec": 20.0,
        "skybot_radius_arcsec": 30,
        "enabled": False,
        "observatory_code": "",
        "observer_name": "",
        "report_dir": "cloud_data/mpc_reports_test",
        "neo_rate_threshold_deg_day": 2.0,
        "comet_extended_fraction": 0.6,
        "rotation_followup": {"enabled": False, "n_visits": 3,
                             "interval_hours": 1.5, "expires_hours": 0.75},
    }
}


def _with_mpc_overrides(**overrides) -> dict:
    """A copy of CONFIG with mpc.<key> overrides — for tests that need a
    specific threshold or to flip rotation_followup on."""
    import copy
    cfg = copy.deepcopy(CONFIG)
    cfg["mpc"].update(overrides)
    return cfg


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


class TrackletGeometryTest(unittest.TestCase):
    """Pure-logic tests for the linking geometry — no database needed."""

    def test_sky_sep_zero_for_identical_point(self):
        self.assertAlmostEqual(
            moving_objects._sky_sep_deg(180.0, 10.0, 180.0, 10.0), 0.0, places=9)

    def test_sky_sep_handles_ra_wraparound(self):
        # 359.5 and 0.5 are 1 deg apart, not 359.
        sep = moving_objects._sky_sep_deg(359.5, 0.0, 0.5, 0.0)
        self.assertAlmostEqual(sep, 1.0, places=3)

    def test_ra_diff_wraparound_safe(self):
        self.assertAlmostEqual(
            moving_objects._ra_diff_deg(0.5, 359.5), 1.0, places=9)
        self.assertAlmostEqual(
            moving_objects._ra_diff_deg(359.5, 0.5), -1.0, places=9)

    def test_cluster_splits_by_field_separation(self):
        near = [{"bjd": 2460500.5, "ra_deg": 180.0, "dec_deg": 10.0},
               {"bjd": 2460500.51, "ra_deg": 180.01, "dec_deg": 10.0}]
        far = [{"bjd": 2460500.5, "ra_deg": 90.0, "dec_deg": -30.0}]
        clusters = moving_objects._cluster_by_time_and_field(
            near + far, window_days=6.0 / 24.0, max_sep_deg=2.0)
        self.assertEqual(len(clusters), 2)
        sizes = sorted(len(c) for c in clusters)
        self.assertEqual(sizes, [1, 2])

    def test_find_linear_track_recovers_clean_line_and_ignores_noise(self):
        t0 = 2460500.5000
        ra0, dec0 = 180.000, 10.000
        ra_rate, dec_rate = 0.05, -0.02   # deg/day
        track = []
        for i in range(4):
            dt = i * 0.02
            track.append({"id": i, "bjd": t0 + dt,
                         "ra_deg": ra0 + ra_rate * dt,
                         "dec_deg": dec0 + dec_rate * dt, "mag": 17.5})
        noise = [
            {"id": 100, "bjd": t0 + 0.01, "ra_deg": ra0 + 0.3, "dec_deg": dec0 - 0.2, "mag": 18.0},
            {"id": 101, "bjd": t0 + 0.05, "ra_deg": ra0 - 0.4, "dec_deg": dec0 + 0.1, "mag": 18.0},
        ]
        found = moving_objects._find_linear_track(
            track + noise, max_resid_arcsec=1.0, min_pts=3)
        self.assertIsNotNone(found)
        found_ids = {d["id"] for d in found}
        self.assertEqual(found_ids, {0, 1, 2, 3})

    def test_find_linear_track_returns_none_below_min_pts(self):
        t0 = 2460500.5
        cluster = [{"id": 0, "bjd": t0, "ra_deg": 180.0, "dec_deg": 10.0, "mag": 17.0},
                  {"id": 1, "bjd": t0 + 0.02, "ra_deg": 180.001, "dec_deg": 10.0, "mag": 17.0}]
        found = moving_objects._find_linear_track(cluster, max_resid_arcsec=1.0, min_pts=3)
        self.assertIsNone(found)

    def test_sky_rate_corrects_ra_for_cos_dec(self):
        # Same raw ra_rate/dec_rate, but at high dec the physical angular
        # rate should be smaller (RA degrees are foreshortened there).
        rate_equator = moving_objects._sky_rate_deg_day(1.0, 0.0, dec0=0.0)
        rate_high_dec = moving_objects._sky_rate_deg_day(1.0, 0.0, dec0=80.0)
        self.assertLess(rate_high_dec, rate_equator)

    def test_classify_track_flags_fast_mover_as_neo_candidate(self):
        track = [{"extended": False}] * 4
        priority, object_type = moving_objects._classify_track(
            track, dec0=0.0, ra_rate=3.0, dec_rate=0.0, config=CONFIG)
        self.assertEqual(priority, "neo_candidate")
        self.assertEqual(object_type, "asteroid")

    def test_classify_track_normal_rate_stays_normal_priority(self):
        track = [{"extended": False}] * 4
        priority, _ = moving_objects._classify_track(
            track, dec0=0.0, ra_rate=0.3, dec_rate=0.1, config=CONFIG)
        self.assertEqual(priority, "normal")

    def test_classify_track_mostly_extended_detections_flagged_comet(self):
        track = [{"extended": True}, {"extended": True}, {"extended": True},
                {"extended": False}]
        _, object_type = moving_objects._classify_track(
            track, dec0=0.0, ra_rate=0.3, dec_rate=0.1, config=CONFIG)
        self.assertEqual(object_type, "comet_candidate")

    def test_classify_track_mostly_point_like_stays_asteroid(self):
        track = [{"extended": False}, {"extended": False}, {"extended": True}]
        _, object_type = moving_objects._classify_track(
            track, dec0=0.0, ra_rate=0.3, dec_rate=0.1, config=CONFIG)
        self.assertEqual(object_type, "asteroid")


class MpcReportFormatTest(unittest.TestCase):
    """Pure formatter tests for the ADES PSV report — no database needed."""

    def _detections(self):
        return [
            {"bjd": 2460500.5000, "ra_deg": 180.123456, "dec_deg": 10.654321,
             "mag": 17.34, "filter": "CV",
             "date_obs_utc": "2024-01-01T00:00:00.000"},
            {"bjd": 2460500.5200, "ra_deg": 180.133456, "dec_deg": 10.634321,
             "mag": 17.40, "filter": "CV",
             "date_obs_utc": "2024-01-01T00:28:48.000"},
            {"bjd": 2460500.5400, "ra_deg": 180.143456, "dec_deg": 10.614321,
             "mag": None, "filter": "CV",
             "date_obs_utc": "2024-01-01T00:57:36.000"},
        ]

    def test_header_and_row_shape(self):
        cand = {"id": 7, "designation": "BS-MP 2024-0007"}
        text = mpc_report._format_ades_psv(cand, self._detections(),
                                           observatory_code="XYZ01",
                                           observer_name="Test Observer")
        lines = text.strip("\n").split("\n")
        header = lines[0].split("|")
        self.assertEqual(header, ["permID", "provID", "trkSub", "mode", "stn",
                                  "obsTime", "ra", "dec", "mag", "band",
                                  "photCat", "notes", "remarks"])
        self.assertEqual(len(lines), 1 + len(self._detections()))
        for row in lines[1:]:
            fields = row.split("|")
            self.assertEqual(len(fields), len(header))

    def test_ra_dec_and_band_formatting(self):
        cand = {"id": 7, "designation": "BS-MP 2024-0007"}
        text = mpc_report._format_ades_psv(cand, self._detections(),
                                           observatory_code="XYZ01",
                                           observer_name="")
        row = text.strip("\n").split("\n")[1].split("|")
        idx = {"trkSub": 2, "stn": 4, "obsTime": 5, "ra": 6, "dec": 7,
              "mag": 8, "band": 9}
        self.assertEqual(row[idx["trkSub"]], "BS-MP 2024-0007")
        self.assertEqual(row[idx["stn"]], "XYZ01")
        self.assertEqual(row[idx["ra"]], "180.123456")
        self.assertEqual(row[idx["dec"]], "+10.654321")
        self.assertEqual(row[idx["band"]], "C")
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", row[idx["obsTime"]]))
        self.assertTrue(row[idx["obsTime"]].endswith("Z"))

    def test_missing_magnitude_yields_blank_field(self):
        cand = {"id": 7, "designation": "BS-MP 2024-0007"}
        text = mpc_report._format_ades_psv(cand, self._detections(),
                                           observatory_code="XYZ01",
                                           observer_name="")
        last_row = text.strip("\n").split("\n")[-1].split("|")
        self.assertEqual(last_row[8], "")   # mag column, third detection has mag=None

    def test_missing_date_obs_falls_back_to_approx_bjd(self):
        cand = {"id": 7, "designation": "BS-MP 2024-0007"}
        dets = [{"bjd": 2460500.5000, "ra_deg": 180.0, "dec_deg": 10.0,
                "mag": 17.0, "filter": "CV", "date_obs_utc": ""}]
        text = mpc_report._format_ades_psv(cand, dets, observatory_code="XYZ01",
                                           observer_name="")
        row = text.strip("\n").split("\n")[1].split("|")
        self.assertNotEqual(row[5], "")             # obsTime still populated
        self.assertIn("approx_time", row[11])        # notes column flags it


@unittest.skipUnless(_postgres_available(), "local postgres not reachable")
class MovingObjectIntegrationTest(unittest.TestCase):
    """record_frame_detections -> link_tracklets -> crossmatch/confirm/reject
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
        for table in ("asteroid_followups", "moving_object_detections",
                     "asteroid_candidates", "mpc_reports", "interrupts"):
            self.db.execute(f"DELETE FROM {table}")

    def _insert_detection(self, node_id, bjd, ra, dec, mag=17.0, snr=20.0,
                          created_at=None, extended=False):
        self.db.execute(
            "INSERT INTO moving_object_detections "
            "(node_id, bjd, ra_deg, dec_deg, mag, mag_err, snr, filter, "
            " frame_id, date_obs_utc, extended, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (node_id, bjd, ra, dec, mag, 0.05, snr, "CV", "",
             "", extended, created_at or datetime.now(timezone.utc).isoformat()))

    def test_record_frame_detections_keeps_only_unmatched_above_snr(self):
        sources = [
            _src(key="CAT-001", matched=True, ra=180.0, dec=10.0),
            _src(key="p180.5+10.0", matched=False, snr=20.0, ra=180.5, dec=10.0),
            _src(key="p181.0+10.0", matched=False, snr=2.0, ra=181.0, dec=10.0),  # below SNR floor
        ]
        n = moving_objects.record_frame_detections(
            "node_a", 2460500.5, "CV", "frame1.fits", "2024-01-01T00:00:00", sources, CONFIG)
        self.assertEqual(n, 1)
        rows = self.db.query("SELECT * FROM moving_object_detections")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["ra_deg"], 180.5)

    def test_link_tracklets_finds_track_and_leaves_noise_unlinked(self):
        t0 = 2460500.5000
        ra0, dec0 = 200.000, -5.000
        ra_rate, dec_rate = 0.04, 0.01
        for i in range(4):
            dt = i * 0.02
            self._insert_detection("node_a", t0 + dt,
                                  ra0 + ra_rate * dt, dec0 + dec_rate * dt)
        # Two unrelated noise detections in the same time/field window.
        self._insert_detection("node_a", t0 + 0.01, ra0 + 0.3, dec0 - 0.25)
        self._insert_detection("node_a", t0 + 0.05, ra0 - 0.4, dec0 + 0.15)

        result = moving_objects.link_tracklets(CONFIG)
        self.assertEqual(result["tracklets"], 1)

        cands = self.db.query("SELECT * FROM asteroid_candidates")
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        self.assertEqual(cand["state"], "linked")
        self.assertEqual(cand["n_detections"], 4)
        self.assertAlmostEqual(cand["ra_rate_deg_day"], ra_rate, places=2)
        self.assertAlmostEqual(cand["dec_rate_deg_day"], dec_rate, places=2)
        self.assertLess(cand["fit_residual_arcsec"], 1.0)

        unlinked = self.db.query(
            "SELECT COUNT(*) AS n FROM moving_object_detections WHERE tracklet_id IS NULL")
        self.assertEqual(unlinked[0]["n"], 2)   # the two noise points

    def test_link_tracklets_no_track_when_too_few_points(self):
        t0 = 2460500.5000
        self._insert_detection("node_b", t0, 100.0, 20.0)
        self._insert_detection("node_b", t0 + 0.02, 100.01, 20.005)
        result = moving_objects.link_tracklets(CONFIG)
        self.assertEqual(result["tracklets"], 0)
        cands = self.db.query("SELECT * FROM asteroid_candidates")
        self.assertEqual(len(cands), 0)

    def test_link_arcs_merges_consistent_multi_night_tracklets(self):
        t0 = 2460500.5000
        ra0, dec0 = 210.000, -8.000
        ra_rate, dec_rate = 0.05, -0.02
        # Night 1: 3 detections over ~0.06 day.
        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_e", t0 + dt,
                                  ra0 + ra_rate * dt, dec0 + dec_rate * dt)
        # Night 2, ~2 days later, consistent with the same linear rate.
        for i in range(3):
            dt = 2.0 + i * 0.02
            self._insert_detection("node_e", t0 + dt,
                                  ra0 + ra_rate * dt, dec0 + dec_rate * dt)
        moving_objects.link_tracklets(CONFIG)
        pre = self.db.query(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_e'")
        self.assertEqual(len(pre), 2)   # two independent single-night tracklets

        result = moving_objects.link_arcs(CONFIG)
        self.assertEqual(result["merged"], 1)

        survivors = self.db.query(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_e' "
            "AND state != 'merged'")
        self.assertEqual(len(survivors), 1)
        arc = survivors[0]
        self.assertEqual(arc["n_detections"], 6)
        self.assertAlmostEqual(arc["ra_rate_deg_day"], ra_rate, places=2)
        self.assertAlmostEqual(arc["dec_rate_deg_day"], dec_rate, places=2)
        detail = self.db.loads(arc.get("detail"), {})
        self.assertEqual(len(detail.get("detection_ids") or []), 6)
        self.assertEqual(len(detail.get("merged_from") or []), 1)

        merged_away = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_e' "
            "AND state = 'merged'")
        self.assertIsNotNone(merged_away)
        merged_detail = self.db.loads(merged_away.get("detail"), {})
        self.assertEqual(merged_detail.get("merged_into"), arc["id"])

        # All 6 detections now point at the surviving tracklet.
        tracklet_ids = self.db.query(
            "SELECT DISTINCT tracklet_id FROM moving_object_detections "
            "WHERE node_id = 'node_e'")
        self.assertEqual(len(tracklet_ids), 1)
        self.assertEqual(tracklet_ids[0]["tracklet_id"], arc["id"])

    def test_link_arcs_does_not_merge_inconsistent_tracks(self):
        t0 = 2460500.5000
        # Night 1: moving one way.
        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_f", t0 + dt, 220.0 + 0.05 * dt, 10.0)
        # Night 2, ~2 days later, but at a wildly different position/rate —
        # a different object, not a continuation of the same arc.
        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_f", t0 + 2.0 + dt, 250.0 + 0.05 * dt, -30.0)
        moving_objects.link_tracklets(CONFIG)
        self.assertEqual(
            len(self.db.query("SELECT * FROM asteroid_candidates WHERE node_id = 'node_f'")), 2)

        result = moving_objects.link_arcs(CONFIG)
        self.assertEqual(result["merged"], 0)
        states = {c["state"] for c in self.db.query(
            "SELECT state FROM asteroid_candidates WHERE node_id = 'node_f'")}
        self.assertEqual(states, {"linked"})

    def test_link_arcs_ignores_known_skybot_tracklets(self):
        t0 = 2460500.5000
        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_g", t0 + dt, 100.0 + 0.05 * dt, 5.0)
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_g'")
        self.db.execute(
            "UPDATE asteroid_candidates SET state = 'known_skybot' WHERE id = %s",
            (cand["id"],))

        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_g", t0 + 2.0 + dt, 100.1 + 0.05 * dt, 5.05)
        moving_objects.link_tracklets(CONFIG)

        result = moving_objects.link_arcs(CONFIG)
        self.assertEqual(result["merged"], 0)

    def test_confirm_and_reject_lifecycle(self):
        t0 = 2460500.5000
        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_c", t0 + dt, 50.0 + 0.03 * dt, 15.0)
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_c'")
        self.assertIsNotNone(cand)
        self.assertEqual(cand["state"], "linked")

        result = moving_objects.confirm_candidate(cand["id"], CONFIG, note="looks real")
        self.assertIsNotNone(result)
        self.assertTrue(result["designation"].startswith("BS-MP "))
        # mpc.enabled is False in CONFIG — report generation is a no-op.
        self.assertIsNone(result["report"])
        updated = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE id = %s", (cand["id"],))
        self.assertEqual(updated["state"], "confirmed")

        # A second candidate, rejected instead.
        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_d", t0 + dt, 60.0 + 0.03 * dt, 25.0)
        moving_objects.link_tracklets(CONFIG)
        cand2 = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_d'")
        self.assertTrue(moving_objects.reject_candidate(cand2["id"], note="not real"))
        updated2 = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE id = %s", (cand2["id"],))
        self.assertEqual(updated2["state"], "rejected")

    def test_link_tracklets_flags_fast_mover_neo_candidate(self):
        t0 = 2460500.5000
        ra0, dec0 = 120.0, 0.0
        ra_rate = 6.0   # deg/day — well above the 2.0 default NEO threshold
        for i in range(4):
            dt = i * 0.01
            self._insert_detection("node_neo", t0 + dt,
                                  ra0 + ra_rate * dt, dec0)
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_neo'")
        self.assertIsNotNone(cand)
        self.assertEqual(cand["priority"], "neo_candidate")

    def test_link_tracklets_normal_rate_is_not_neo_candidate(self):
        t0 = 2460500.5000
        for i in range(4):
            dt = i * 0.02
            self._insert_detection("node_slow", t0 + dt, 130.0 + 0.03 * dt, 0.0)
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_slow'")
        self.assertEqual(cand["priority"], "normal")

    def test_link_tracklets_flags_mostly_extended_track_comet_candidate(self):
        t0 = 2460500.5000
        for i in range(4):
            dt = i * 0.02
            self._insert_detection("node_comet", t0 + dt, 140.0 + 0.03 * dt, 0.0,
                                  extended=(i != 3))   # 3 of 4 extended
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_comet'")
        self.assertEqual(cand["object_type"], "comet_candidate")

    def test_link_tracklets_all_point_like_stays_asteroid(self):
        t0 = 2460500.5000
        for i in range(4):
            dt = i * 0.02
            self._insert_detection("node_ast", t0 + dt, 150.0 + 0.03 * dt, 0.0)
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_ast'")
        self.assertEqual(cand["object_type"], "asteroid")

    def test_confirm_schedules_rotation_followup_for_normal_asteroid(self):
        t0 = 2460500.5000
        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_h", t0 + dt, 70.0 + 0.03 * dt, 15.0)
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_h'")

        cfg = _with_mpc_overrides(
            rotation_followup={"enabled": True, "n_visits": 3,
                              "interval_hours": 1.0, "expires_hours": 0.5})
        result = moving_objects.confirm_candidate(cand["id"], cfg)
        self.assertEqual(result["followups_scheduled"], 3)

        rows = self.db.query(
            "SELECT * FROM asteroid_followups WHERE candidate_id = %s "
            "ORDER BY seq", (cand["id"],))
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[0]["node_id"], "node_h")
        self.assertIsNone(rows[0]["fired_at"])
        now = datetime.now(timezone.utc)
        for r in rows:
            not_before = datetime.fromisoformat(r["not_before"])
            self.assertGreater(not_before, now)

    def test_confirm_skips_rotation_followup_for_neo_candidate(self):
        t0 = 2460500.5000
        ra_rate = 6.0
        for i in range(3):
            dt = i * 0.01
            self._insert_detection("node_i", t0 + dt, 80.0 + ra_rate * dt, 15.0)
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_i'")
        self.assertEqual(cand["priority"], "neo_candidate")

        cfg = _with_mpc_overrides(rotation_followup={"enabled": True, "n_visits": 3,
                                                     "interval_hours": 1.0,
                                                     "expires_hours": 0.5})
        result = moving_objects.confirm_candidate(cand["id"], cfg)
        self.assertEqual(result["followups_scheduled"], 0)
        rows = self.db.query(
            "SELECT * FROM asteroid_followups WHERE candidate_id = %s", (cand["id"],))
        self.assertEqual(len(rows), 0)

    def test_dispatch_due_followups_fires_due_slot_as_interrupt(self):
        t0 = 2460500.5000
        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_j", t0 + dt, 90.0 + 0.03 * dt, 15.0)
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_j'")
        moving_objects.confirm_candidate(cand["id"], CONFIG)   # rotation_followup off

        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.db.execute(
            "INSERT INTO asteroid_followups "
            "(candidate_id, node_id, seq, not_before, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (cand["id"], "node_j", 1, past, datetime.now(timezone.utc).isoformat()))

        result = moving_objects.dispatch_due_followups(CONFIG)
        self.assertEqual(result["fired"], 1)
        self.assertEqual(result["skipped"], 0)

        followup = self.db.query_one(
            "SELECT * FROM asteroid_followups WHERE candidate_id = %s", (cand["id"],))
        self.assertIsNotNone(followup["fired_at"])
        self.assertIsNotNone(followup["interrupt_id"])

        interrupt = self.db.query_one(
            "SELECT * FROM interrupts WHERE id = %s", (followup["interrupt_id"],))
        self.assertIsNotNone(interrupt)
        self.assertEqual(interrupt["reason"], "asteroid_followup")
        self.assertEqual(self.db.loads(interrupt["node_ids"], []), ["node_j"])

    def test_dispatch_due_followups_skips_no_longer_confirmed_candidate(self):
        t0 = 2460500.5000
        for i in range(3):
            dt = i * 0.02
            self._insert_detection("node_k", t0 + dt, 100.0 + 0.03 * dt, 15.0)
        moving_objects.link_tracklets(CONFIG)
        cand = self.db.query_one(
            "SELECT * FROM asteroid_candidates WHERE node_id = 'node_k'")
        # Never confirmed (still 'linked') — a stray follow-up row for it
        # should be skipped, not fired as a live interrupt.
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.db.execute(
            "INSERT INTO asteroid_followups "
            "(candidate_id, node_id, seq, not_before, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (cand["id"], "node_k", 1, past, datetime.now(timezone.utc).isoformat()))

        result = moving_objects.dispatch_due_followups(CONFIG)
        self.assertEqual(result["fired"], 0)
        self.assertEqual(result["skipped"], 1)
        followup = self.db.query_one(
            "SELECT * FROM asteroid_followups WHERE candidate_id = %s", (cand["id"],))
        self.assertIsNotNone(followup["fired_at"])
        self.assertIsNone(followup["interrupt_id"])
        self.assertEqual(
            self.db.query_one("SELECT COUNT(*) AS n FROM interrupts")["n"], 0)

    def test_stale_unlinked_detections_retired_so_scan_set_cannot_starve(self):
        # Old noise that will never link: without retirement it would clog the
        # linker's LIMIT forever and starve genuinely new detections.
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        for i in range(5):
            self._insert_detection("node_noise", 2460000.0 + i,
                                  10.0 + i, 5.0 + i, created_at=old)
        moving_objects.link_tracklets(CONFIG)
        retired = self.db.query_one(
            "SELECT COUNT(*) AS n FROM moving_object_detections "
            "WHERE link_done = TRUE")
        self.assertEqual(retired["n"], 5)
        active = self.db.query_one(
            "SELECT COUNT(*) AS n FROM moving_object_detections "
            "WHERE tracklet_id IS NULL AND link_done = FALSE")
        self.assertEqual(active["n"], 0)   # nothing left to re-scan next pass

    def test_recent_unlinked_detections_are_not_retired(self):
        # Fresh singletons might still gain a partner from a later frame — they
        # must stay in the scan set (not retired) until their window closes.
        for i in range(2):
            self._insert_detection("node_fresh", 2460500.5 + i * 0.01,
                                  30.0 + i, 15.0)
        moving_objects.link_tracklets(CONFIG)
        active = self.db.query_one(
            "SELECT COUNT(*) AS n FROM moving_object_detections "
            "WHERE link_done = FALSE")
        self.assertEqual(active["n"], 2)

    def test_prune_removes_retired_and_closed_but_keeps_open(self):
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        # Retired noise, old -> pruned.
        self.db.execute(
            "INSERT INTO moving_object_detections "
            "(node_id, bjd, ra_deg, dec_deg, mag, mag_err, snr, filter, "
            " frame_id, date_obs_utc, link_done, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("n1", 2460000.0, 1.0, 1.0, 18.0, 0.05, 6.0, "CV", "", "", True, old))
        # Retired noise but recent -> kept.
        self.db.execute(
            "INSERT INTO moving_object_detections "
            "(node_id, bjd, ra_deg, dec_deg, mag, mag_err, snr, filter, "
            " frame_id, date_obs_utc, link_done, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("n2", 2460001.0, 2.0, 2.0, 18.0, 0.05, 6.0, "CV", "", "", True, recent))
        # Open (unlinked, not retired), old -> kept (still linkable/audit).
        self._insert_detection("n3", 2460002.0, 3.0, 3.0, created_at=old)

        deleted = moving_objects.prune_detections(CONFIG)
        self.assertEqual(deleted, 1)
        remaining = {r["node_id"] for r in self.db.query(
            "SELECT node_id FROM moving_object_detections")}
        self.assertEqual(remaining, {"n2", "n3"})


if __name__ == "__main__":
    unittest.main()
