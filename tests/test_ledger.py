#!/usr/bin/env python3
"""
CHORUS T0 ledger — cloud.chorus.ledger.refresh_node, focused on the
chronic-dropout quarantine addition: a night cloud.chorus.reflow had to move
a node's own remaining plan items elsewhere (and the node delivered nothing
that night) is failure signal a plain zero-delivery night can't supply on
its own, and it escalates once a node racks up enough distinct dropout
nights to look like a recurring fault rather than one cloudy night.

Run with:  python3 -m unittest tests.test_ledger
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from cloud.chorus import ledger

TEST_DB_NAME = "boundless_ledger_test"
ADMIN_URL = "postgresql://boundless@/boundless?host=/tmp"
TEST_URL = f"postgresql://boundless@/{TEST_DB_NAME}?host=/tmp"


def _postgres_available() -> bool:
    try:
        import psycopg2
        psycopg2.connect(ADMIN_URL).close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_postgres_available(), "local postgres not reachable")
class LedgerDropoutQuarantineTest(unittest.TestCase):
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
        for t in ("chorus_node_ledger", "reflow_log", "measurements", "plans",
                  "reliability_incidents"):
            self.db.execute(f"DELETE FROM {t}")

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def _night_str(self, days_ago: int) -> str:
        return (datetime.now(timezone.utc)
               - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    def _reflow_row(self, node_id, night, created_at=None):
        self.db.execute(
            "INSERT INTO reflow_log (night, from_node, to_node, target_id, "
            " target_name, expected_info, outcome, created_at) "
            "VALUES (%s,%s,'nd_other','T1','V1',0.4,'dispatched',%s)",
            (night, node_id, created_at or self._now()))

    def _detail(self, node_id):
        row = self.db.query_one(
            "SELECT * FROM chorus_node_ledger WHERE node_id = %s", (node_id,))
        self.assertIsNotNone(row)
        return row, self.db.loads(row.get("detail"), {})

    def test_no_dropouts_baseline(self):
        ledger.refresh_node({"node_id": "nd_clean"})
        row, detail = self._detail("nd_clean")
        self.assertEqual(detail["dropout_nights"], 0)
        self.assertFalse(detail["dropout_quarantined"])
        # Beta(4,2) prior, untouched.
        self.assertAlmostEqual(float(row["p_exec_a"]), ledger.PRIOR_A)
        self.assertAlmostEqual(float(row["p_exec_b"]), ledger.PRIOR_B)

    def test_single_dropout_night_adds_small_penalty(self):
        self._reflow_row("nd_one_drop", self._night_str(1))
        ledger.refresh_node({"node_id": "nd_one_drop"})
        row, detail = self._detail("nd_one_drop")
        self.assertEqual(detail["dropout_nights"], 1)
        self.assertFalse(detail["dropout_quarantined"])
        self.assertAlmostEqual(
            float(row["p_exec_b"]), ledger.PRIOR_B + ledger.DROPOUT_FAIL_WEIGHT,
            places=2)

    def test_chronic_dropouts_trigger_quarantine_multiplier(self):
        node_id = "nd_chronic"
        for d in range(1, ledger.DROPOUT_STREAK_THRESHOLD + 1):
            self._reflow_row(node_id, self._night_str(d))
        ledger.refresh_node({"node_id": node_id})
        row, detail = self._detail(node_id)
        self.assertEqual(detail["dropout_nights"], ledger.DROPOUT_STREAK_THRESHOLD)
        self.assertTrue(detail["dropout_quarantined"])
        expected_b = (ledger.PRIOR_B + ledger.DROPOUT_STREAK_THRESHOLD
                     * ledger.DROPOUT_FAIL_WEIGHT * ledger.DROPOUT_QUARANTINE_MULTIPLIER)
        self.assertAlmostEqual(float(row["p_exec_b"]), expected_b, places=1)

        # p_exec (the mean of the Beta posterior) is depressed relative to a
        # node with a single, non-chronic dropout.
        self._reflow_row("nd_one_drop2", self._night_str(1))
        ledger.refresh_node({"node_id": "nd_one_drop2"})
        row2, _ = self._detail("nd_one_drop2")
        p_exec_single, _ = ledger._beta_stats(
            float(row2["p_exec_a"]), float(row2["p_exec_b"]))
        p_exec_chronic_mean, _ = ledger._beta_stats(
            float(row["p_exec_a"]), float(row["p_exec_b"]))
        self.assertLess(p_exec_chronic_mean, p_exec_single)

    def test_dropout_night_with_delivery_is_not_double_counted(self):
        # The node delivered a measurement on the same night reflow_log shows
        # a dropout for it (e.g. it recovered partway through the night) —
        # that night must not count as a dropout on top of the normal
        # per-item p_exec accounting.
        node_id = "nd_recovered"
        night = self._night_str(1)
        self.db.execute(
            "INSERT INTO measurements (node_id, target_name, bjd, magnitude, "
            " uncertainty, received_at) VALUES (%s,'V1',2460500.5,13.0,0.05,%s)",
            (node_id, night + "T04:00:00+00:00"))
        self._reflow_row(node_id, night, created_at=night + "T02:00:00+00:00")
        ledger.refresh_node({"node_id": node_id})
        _, detail = self._detail(node_id)
        self.assertEqual(detail["dropout_nights"], 0)


if __name__ == "__main__":
    unittest.main()
