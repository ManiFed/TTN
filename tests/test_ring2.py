#!/usr/bin/env python3
"""
CHORUS Ring 2 — structural evolution (cloud.chorus.ring2), against a real
(throwaway) PostgreSQL database.

Builds chorus_run_archive rows by hand (in the exact JSON shape
backtest.archive_run would have produced, including target_raw/span_t0/
span_t1) rather than running a full CHORUS plan, so the backtest-replay
math is exercised directly and deterministically.

Run with:  python3 -m unittest tests.test_ring2
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from cloud.chorus import ring2

TEST_DB_NAME = "boundless_ring2_test"
ADMIN_URL = "postgresql://boundless@/boundless?host=/tmp"
TEST_URL = f"postgresql://boundless@/{TEST_DB_NAME}?host=/tmp"

BASE = datetime(2026, 6, 27, 22, 0, tzinfo=timezone.utc)
END = BASE + timedelta(hours=8)


def _postgres_available() -> bool:
    try:
        import psycopg2
        psycopg2.connect(ADMIN_URL).close()
        return True
    except Exception:
        return False


def _ctx_dict():
    return {"t0": BASE.isoformat(), "t1": END.isoformat(), "n_slots": 32,
           "utc_offset_h": 0.0, "min_alt": 25.0, "mount_type": "alt_az",
           "filters": ["CV", "B"], "max_targets": 4, "lat": 40.0, "lon": 0.0}


def _opp_dict(filter="B"):
    return {"node_id": "A", "target_id": "T1", "name": "T1",
           "ra": 10.0, "dec": 40.0, "mag": 12.0, "type": "VAR",
           "t_sub": 10.0, "n_sub": 30, "dwell": 5.0, "sigma": 0.02, "need": 1,
           "slots": {"0": [0.95, 0.02, 60.0, 120.0]},
           "filter": filter, "p_exec": 0.9, "p_accept": 0.95, "explore": 0.0,
           "lon": 0.0, "variant": "epoch", "mode": "single_epoch",
           "dur": 5.0, "seq": 1, "eph": None}


def _target_dict():
    return {"target_id": "T1", "name": "T1", "target_type": "VAR",
           "priority": 0.8, "cadence_hours": 4.0, "mag": 12.0,
           "ra_deg": 10.0, "dec_deg": 40.0}


def _inputs():
    return {
        "seed": 7,
        "contexts": {"A": _ctx_dict()},
        "cells": {},
        "opps": [_opp_dict(filter="B")],
        "params": {},
        "target_raw": {"T1": {"target": _target_dict(), "state": {},
                             "scarcity": 1.0}},
        "band_union": ["B"],
        "span_t0": BASE.isoformat(),
        "span_t1": END.isoformat(),
    }


def _realized():
    return {"A": {"T1": {"accepted": True, "sigma": 0.02}}}


@unittest.skipUnless(_postgres_available(), "local postgres not reachable")
class Ring2Test(unittest.TestCase):
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
        for t in ("class_templates", "chorus_run_archive"):
            self.db.execute(f"DELETE FROM {t}")

    def _seed_archive(self, n: int, night_prefix="2026-06-2"):
        for i in range(n):
            self.db.execute(
                """INSERT INTO chorus_run_archive
                       (run_id, night, ran_at, inputs, phi_expected, realized,
                        shadow)
                   VALUES (%s,%s,%s,%s,%s,%s,0)""",
                (f"run_{i}", f"{night_prefix}{i}",
                 datetime.now(timezone.utc).isoformat(),
                 json.dumps(_inputs()), 1.0, json.dumps(_realized())))

    # ── active_templates / propose_template ─────────────────────────────────────

    def test_active_templates_empty_with_no_rows(self):
        self.assertEqual(ring2.active_templates(), {})

    def test_active_templates_only_reflects_live_stage(self):
        self.db.execute(
            "INSERT INTO class_templates (family, params, stage, created_at, "
            "updated_at) VALUES ('default', %s, 'advisory', %s, %s)",
            (json.dumps({"band_value_mult": 0.9}),
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()))
        self.assertEqual(ring2.active_templates(), {})

        self.db.execute(
            "INSERT INTO class_templates (family, params, stage, created_at, "
            "updated_at) VALUES ('default', %s, 'live', %s, %s)",
            (json.dumps({"band_value_mult": 0.7}),
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()))
        self.assertEqual(ring2.active_templates(),
                         {"default": {"band_value_mult": 0.7}})

    def test_active_templates_newest_live_row_wins(self):
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO class_templates (family, params, stage, created_at, "
            "updated_at) VALUES ('default', %s, 'live', %s, %s)",
            (json.dumps({"band_value_mult": 0.5}), now, now))
        self.db.execute(
            "INSERT INTO class_templates (family, params, stage, created_at, "
            "updated_at) VALUES ('default', %s, 'live', %s, %s)",
            (json.dumps({"band_value_mult": 0.9}), now, now))
        self.assertEqual(ring2.active_templates(),
                         {"default": {"band_value_mult": 0.9}})

    def test_propose_template_inserts_advisory_row(self):
        tid = ring2.propose_template("default", {"band_value_mult": 0.9},
                                     note="testing")
        row = self.db.query_one(
            "SELECT * FROM class_templates WHERE id = %s", (tid,))
        self.assertEqual(row["stage"], "advisory")
        self.assertEqual(row["family"], "default")
        self.assertEqual(self.db.loads(row["params"], {}),
                         {"band_value_mult": 0.9})

    # ── gate_template ────────────────────────────────────────────────────────────

    def test_gate_rejects_when_not_enough_replayable_runs(self):
        self._seed_archive(1)   # below MIN_RUNS_TO_GATE
        ok, detail = ring2.gate_template("default", {"band_value_mult": 0.9}, {})
        self.assertFalse(ok)
        self.assertIn("reason", detail)

    def test_gate_allows_a_genuinely_better_template(self):
        from cloud.chorus import backtest
        self._seed_archive(backtest.MIN_RUNS_TO_GATE)
        # Higher band_value_mult than the hardcoded 0.4 default -> the B-band
        # cell (the only cell this opportunity's filter can capture) is
        # worth strictly more, so realized_phi_candidate must be higher.
        ok, detail = ring2.gate_template("default", {"band_value_mult": 0.8}, {})
        self.assertTrue(ok, detail)
        self.assertGreater(detail["realized_phi_candidate"],
                           detail["realized_phi_current"])

    def test_gate_rejects_a_worse_template(self):
        from cloud.chorus import backtest
        self._seed_archive(backtest.MIN_RUNS_TO_GATE)
        ok, detail = ring2.gate_template("default", {"band_value_mult": 0.01}, {})
        self.assertFalse(ok, detail)
        self.assertLess(detail["realized_phi_candidate"],
                       detail["realized_phi_current"])

    # ── run_pending: fully autonomous apply/reject ──────────────────────────────

    def test_run_pending_promotes_a_passing_proposal_to_live(self):
        from cloud.chorus import backtest
        self._seed_archive(backtest.MIN_RUNS_TO_GATE)
        tid = ring2.propose_template("default", {"band_value_mult": 0.8})
        result = ring2.run_pending({})
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["rejected"], 0)
        row = self.db.query_one(
            "SELECT * FROM class_templates WHERE id = %s", (tid,))
        self.assertEqual(row["stage"], "live")
        self.assertEqual(ring2.active_templates(),
                         {"default": {"band_value_mult": 0.8}})

    def test_run_pending_rejects_a_failing_proposal_with_detail_attached(self):
        from cloud.chorus import backtest
        self._seed_archive(backtest.MIN_RUNS_TO_GATE)
        tid = ring2.propose_template("default", {"band_value_mult": 0.01})
        result = ring2.run_pending({})
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["rejected"], 1)
        row = self.db.query_one(
            "SELECT * FROM class_templates WHERE id = %s", (tid,))
        self.assertEqual(row["stage"], "rejected")
        detail = self.db.loads(row["backtest_detail"], {})
        self.assertIn("realized_phi_current", detail)
        # A human can inspect why, but nothing was waiting on their approval.
        self.assertEqual(ring2.active_templates(), {})

    def test_run_pending_no_proposals_is_a_no_op(self):
        result = ring2.run_pending({})
        self.assertEqual(result, {"applied": 0, "rejected": 0})


if __name__ == "__main__":
    unittest.main()
