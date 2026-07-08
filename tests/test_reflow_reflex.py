#!/usr/bin/env python3
"""
THE ORGANISM Phase 2 — reflow + reflex confirmation.

Two layers, mirroring the survey tests:

  ReflowGreedyTest — the CHORUS-reuse core (reflow.greedy_place) as pure logic
    over synthetic CHORUS objects: it must route a target to the better-placed
    dark node and de-conflict one placement per target, using the same best_slot
    marginal the contingency ladder uses. No DB, no weather.

  ReflowReflexCloudTest — dispatch + guardrails against a throwaway PostgreSQL:
    reflex opens a targeted interrupt at dark nodes and honours its caps;
    reflow.dispatch_reflow writes interrupts + reflow_log; detect_dropouts reads
    the live map. No telescopes.

Run with:  python3 -m unittest tests.test_reflow_reflex
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from cloud.chorus import reflow

# Reuse the CHORUS synthetic builders.
from tests.test_chorus import _cell, _ctx, _opp, _params

TEST_DB_NAME = "boundless_reflow_test"
ADMIN_URL = "postgresql://boundless@/boundless?host=/tmp"
TEST_URL = f"postgresql://boundless@/{TEST_DB_NAME}?host=/tmp"


def _postgres_available() -> bool:
    try:
        import psycopg2
        psycopg2.connect(ADMIN_URL).close()
        return True
    except Exception:
        return False


class ReflowGreedyTest(unittest.TestCase):
    """reflow.greedy_place reuses CHORUS best_slot faithfully."""

    def test_routes_to_better_placed_node(self):
        # Two dark candidate nodes; node B sees the target under clearer sky
        # (higher p_sky → higher delivery p → higher marginal). Reflow must pick B.
        ctxs = {"A": _ctx("A"), "B": _ctx("B")}
        cells = {"T1": [_cell("T1")]}
        opps = {
            "A": [_opp("A", "T1", p_sky=0.30)],
            "B": [_opp("B", "T1", p_sky=0.98)],
        }
        placed = reflow.greedy_place(ctxs, opps, cells, _params())
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0]["to_node"], "B")
        self.assertEqual(placed[0]["target_id"], "T1")
        self.assertGreater(placed[0]["expected_info"], 0.0)

    def test_one_placement_per_target(self):
        # A single target with opps on both nodes yields exactly one placement.
        ctxs = {"A": _ctx("A"), "B": _ctx("B")}
        cells = {"T1": [_cell("T1")]}
        opps = {"A": [_opp("A", "T1")], "B": [_opp("B", "T1")]}
        placed = reflow.greedy_place(ctxs, opps, cells, _params())
        self.assertEqual([p["target_id"] for p in placed], ["T1"])

    def test_two_targets_spread_across_nodes(self):
        # Node A best for T1, node B best for T2 — reflow places both.
        ctxs = {"A": _ctx("A", max_targets=2), "B": _ctx("B", max_targets=2)}
        cells = {"T1": [_cell("T1")], "T2": [_cell("T2")]}
        opps = {
            "A": [_opp("A", "T1", p_sky=0.98), _opp("A", "T2", p_sky=0.30)],
            "B": [_opp("B", "T1", p_sky=0.30), _opp("B", "T2", p_sky=0.98)],
        }
        placed = {p["target_id"]: p["to_node"]
                  for p in reflow.greedy_place(ctxs, opps, cells, _params())}
        self.assertEqual(placed, {"T1": "A", "T2": "B"})

    def test_no_feasible_slot_yields_nothing(self):
        # An opportunity with no slots can't be placed.
        ctxs = {"A": _ctx("A")}
        cells = {"T1": [_cell("T1")]}
        opp = _opp("A", "T1")
        opp.slots = {}
        placed = reflow.greedy_place(ctxs, {"A": [opp]}, cells, _params())
        self.assertEqual(placed, [])


@unittest.skipUnless(_postgres_available(), "local postgres not reachable")
class ReflowReflexCloudTest(unittest.TestCase):
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
        for t in ("interrupts", "reflow_log", "node_live_state",
                  "dispatch_events", "discovery_candidates", "plans", "nodes",
                  "incidents", "measurements", "targets"):
            self.db.execute(f"DELETE FROM {t}")
        from cloud import reflex
        reflex._last_fire.clear()

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def _dark_node(self, node_id, ra=180.0, dec=40.0):
        from cloud import live
        # A registered, dark, online node the selector can rank.
        self.db.execute(
            """INSERT INTO nodes (node_id, api_key, latitude, longitude,
                   registered_at, last_heartbeat, status)
               VALUES (%s,%s,%s,%s,%s,%s,'active')""",
            (node_id, "k", dec, ra, self._now(), self._now()))
        live.record_state(node_id, {"phase": "idle", "is_dark": True}, heartbeat_s=5)

    # ── Reflex ──────────────────────────────────────────────────────────────────

    def _config(self, **reflex_over):
        r = {"enabled": True, "max_per_night_global": 30, "max_per_candidate": 1,
             "min_peak_delta_mag": 0.5, "cooldown_min": 120, "n_nodes": 3,
             "expires_hours": 2}
        r.update(reflex_over)
        return {"survey": {"reflex": r},
                "scoring": {}, "scheduler": {}}

    def _cand(self, **kw):
        c = {"source_key": "OA-1", "ra_deg": 180.0, "dec_deg": 40.0,
             "kind": "brightening", "peak_delta_mag": 1.2, "last_mag": 13.0}
        c.update(kw)
        return c

    def test_reflex_opens_targeted_interrupt_at_dark_node(self):
        from cloud import reflex
        self._dark_node("nd_dark")
        fired = reflex.on_candidate_promoted(self._cand(), self._config())
        self.assertTrue(fired)
        row = self.db.query_one(
            "SELECT * FROM interrupts WHERE reason = 'reflex_confirm'")
        self.assertIsNotNone(row)
        self.assertIn("nd_dark", json.loads(row["node_ids"]))
        # And a push was logged for the confirming node.
        self.assertEqual(self.db.query_one(
            "SELECT COUNT(*) AS n FROM dispatch_events "
            "WHERE node_id='nd_dark' AND kind='interrupt'")["n"], 1)

    def test_reflex_skips_small_delta(self):
        from cloud import reflex
        self._dark_node("nd_dark")
        self.assertFalse(
            reflex.on_candidate_promoted(self._cand(peak_delta_mag=0.2),
                                         self._config()))
        self.assertIsNone(self.db.query_one("SELECT 1 FROM interrupts"))

    def test_reflex_no_dark_nodes_no_fire(self):
        from cloud import reflex
        # Node exists but is in daylight → not eligible.
        from cloud import live
        self.db.execute(
            """INSERT INTO nodes (node_id, api_key, latitude, longitude,
                   registered_at, last_heartbeat, status)
               VALUES ('nd_day','k',40,180,%s,%s,'active')""",
            (self._now(), self._now()))
        live.record_state("nd_day", {"phase": "daylight", "is_dark": False})
        self.assertFalse(reflex.on_candidate_promoted(self._cand(), self._config()))

    def test_reflex_global_cap(self):
        from cloud import reflex
        self._dark_node("nd_dark")
        cfg = self._config(max_per_night_global=1, cooldown_min=0)
        self.assertTrue(reflex.on_candidate_promoted(self._cand(source_key="A"), cfg))
        # Second distinct source blocked by the global nightly cap.
        self.assertFalse(reflex.on_candidate_promoted(self._cand(source_key="B"), cfg))

    def test_reflex_dedupes_open_interrupt(self):
        from cloud import reflex
        self._dark_node("nd_dark")
        cfg = self._config(cooldown_min=0)
        self.assertTrue(reflex.on_candidate_promoted(self._cand(), cfg))
        # Same source with an interrupt still open → no duplicate.
        self.assertFalse(reflex.on_candidate_promoted(self._cand(), cfg))
        self.assertEqual(self.db.query_one(
            "SELECT COUNT(*) AS n FROM interrupts")["n"], 1)

    # ── Reflow dispatch + detection ─────────────────────────────────────────────

    def test_dispatch_reflow_writes_interrupt_log_and_push(self):
        placements = [{
            "to_node": "nd_b", "target_id": "T1", "target_name": "V1",
            "ra_deg": 120.0, "dec_deg": 5.0, "mag": 14.0, "expected_info": 0.42,
        }]
        n = reflow.dispatch_reflow("nd_a", placements, {})
        self.assertEqual(n, 1)
        iv = self.db.query_one("SELECT * FROM interrupts WHERE reason='reflow'")
        self.assertEqual(json.loads(iv["node_ids"]), ["nd_b"])
        log = self.db.query_one("SELECT * FROM reflow_log")
        self.assertEqual((log["from_node"], log["to_node"]), ("nd_a", "nd_b"))
        self.assertEqual(log["interrupt_id"], iv["id"])
        self.assertEqual(self.db.query_one(
            "SELECT COUNT(*) AS n FROM dispatch_events "
            "WHERE node_id='nd_b' AND kind='interrupt'")["n"], 1)

    def test_detect_dropouts_finds_clouded_node_with_remaining_plan(self):
        from cloud import live
        plan = {"items": [
            {"target_id": "T1", "target": "a"},
            {"target_id": "T2", "target": "b"},
            {"target_id": "T3", "target": "c"}]}
        self.db.execute(
            """INSERT INTO plans (plan_id, node_id, night, generated_at,
                   plan_json, status)
               VALUES ('p1','nd_x','2026-07-07',%s,%s,'current')""",
            (self._now(), json.dumps(plan)))
        # Node clouded out having finished item index 0 — T2, T3 remain.
        live.record_state("nd_x", {"phase": "clouded", "is_dark": True,
                                   "plan_item_idx": 0})
        drop = reflow.detect_dropouts({"scheduler": {}})
        self.assertEqual(len(drop), 1)
        self.assertEqual(drop[0]["node_id"], "nd_x")
        self.assertEqual({r["target_id"] for r in drop[0]["remaining"]},
                         {"T2", "T3"})

    def test_detect_dropouts_ignores_healthy_node(self):
        from cloud import live
        self.db.execute(
            """INSERT INTO plans (plan_id, node_id, night, generated_at,
                   plan_json, status)
               VALUES ('p2','nd_ok','2026-07-07',%s,%s,'current')""",
            (self._now(), json.dumps({"items": [{"target_id": "T1"}]})))
        live.record_state("nd_ok", {"phase": "exposing", "is_dark": True,
                                    "plan_item_idx": 0})
        self.assertEqual(reflow.detect_dropouts({"scheduler": {}}), [])

    def test_open_critical_incident_is_a_dropout(self):
        # Self-healing: a node with an OPEN CRITICAL incident is a dropout even
        # if its live phase still looks healthy.
        from cloud import live
        self.db.execute(
            """INSERT INTO plans (plan_id, node_id, night, generated_at,
                   plan_json, status)
               VALUES ('p3','nd_sick','2026-07-07',%s,%s,'current')""",
            (self._now(), json.dumps({"items": [{"target_id": "T1"},
                                                {"target_id": "T2"}]})))
        live.record_state("nd_sick", {"phase": "exposing", "is_dark": True,
                                      "plan_item_idx": 0})
        self.db.execute(
            "INSERT INTO incidents (node_id, status, title, severity, "
            " opened_at, updated_at) VALUES "
            "('nd_sick','open','emergency park','critical',%s,%s)",
            (self._now(), self._now()))
        drop = reflow.detect_dropouts({"scheduler": {}})
        self.assertEqual([d["node_id"] for d in drop], ["nd_sick"])

    def test_reconcile_marks_delivered_and_missed(self):
        # Two reflows tonight; only one node delivered a measurement.
        night = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.db.execute(
            "INSERT INTO targets (target_id, name, ra_deg, dec_deg) "
            "VALUES ('T1','V1',10,20)")
        for tgt, to_node in (("T1", "nd_hit"), ("T1", "nd_miss")):
            self.db.execute(
                "INSERT INTO reflow_log (night, from_node, to_node, target_id, "
                " target_name, expected_info, outcome, created_at) "
                "VALUES (%s,'nd_a',%s,%s,'V1',0.4,'dispatched',%s)",
                (night, to_node, tgt, self._now()))
        # nd_hit produced a measurement for V1 after the reflow.
        self.db.execute(
            "INSERT INTO measurements (node_id, target_name, bjd, magnitude, "
            " uncertainty, received_at) VALUES "
            "('nd_hit','V1',2460500.5,13.0,0.05,%s)", (self._now(),))
        res = reflow.reconcile_outcomes({})
        self.assertEqual(res["delivered"], 1)
        self.assertEqual(res["missed"], 1)
        hit = self.db.query_one(
            "SELECT outcome FROM reflow_log WHERE to_node='nd_hit'")
        miss = self.db.query_one(
            "SELECT outcome FROM reflow_log WHERE to_node='nd_miss'")
        self.assertEqual(hit["outcome"], "delivered")
        self.assertEqual(miss["outcome"], "missed")


if __name__ == "__main__":
    unittest.main()
