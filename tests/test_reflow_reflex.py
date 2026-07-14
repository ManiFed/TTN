#!/usr/bin/env python3
"""
Live fleet Phase 2 — reflow + reflex confirmation.

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

    def test_eps_scale_relaxes_min_marginal_floor(self):
        # A min_marginal floor set high enough to reject every real placement
        # at eps_scale=1.0 (the default, one-off-dropout case)...
        ctxs = {"A": _ctx("A")}
        cells = {"T1": [_cell("T1")]}
        opps = {"A": [_opp("A", "T1")]}
        params = _params(min_marginal=999.0)
        self.assertEqual(reflow.greedy_place(ctxs, opps, cells, params), [])
        # ...but a relaxed eps_scale (chronic dropout escalation) lowers the
        # floor to 0, so the same opportunity is accepted.
        placed = reflow.greedy_place(ctxs, opps, cells, params, eps_scale=0.0)
        self.assertEqual([p["target_id"] for p in placed], ["T1"])


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
        self.assertEqual(log["dark_streak"], 0)
        self.assertEqual(self.db.query_one(
            "SELECT COUNT(*) AS n FROM dispatch_events "
            "WHERE node_id='nd_b' AND kind='interrupt'")["n"], 1)

    def test_dispatch_reflow_records_dark_streak(self):
        placements = [{
            "to_node": "nd_b", "target_id": "T1", "target_name": "V1",
            "ra_deg": 120.0, "dec_deg": 5.0, "mag": 14.0, "expected_info": 0.42,
        }]
        reflow.dispatch_reflow("nd_a", placements, {}, dark_streak=4)
        log = self.db.query_one("SELECT * FROM reflow_log")
        self.assertEqual(log["dark_streak"], 4)

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
        # No prior measurements at all -> treated as a fresh (streak=0 is not
        # guaranteed since it's "never seen", so just check the key exists
        # and is a non-negative int).
        self.assertIsInstance(drop[0]["dark_streak"], int)
        self.assertGreaterEqual(drop[0]["dark_streak"], 0)

    def test_remaining_items_filters_out_deactivated_targets(self):
        # A stale plan (node hasn't been replanned in a while) can reference a
        # target that's since been deactivated — reflow must not offer it.
        self.db.execute(
            "INSERT INTO targets (target_id, name, ra_deg, dec_deg, active) "
            "VALUES ('T1','V1',10,20,1), ('T2','V2',30,40,0)")
        plan = {"items": [{"target_id": "T1", "target": "V1"},
                          {"target_id": "T2", "target": "V2"}]}
        self.db.execute(
            """INSERT INTO plans (plan_id, node_id, night, generated_at,
                   plan_json, status)
               VALUES ('p_stale','nd_stale','2026-07-01',%s,%s,'current')""",
            (self._now(), json.dumps(plan)))
        remaining = reflow._remaining_items("nd_stale", None)
        self.assertEqual([r["target_id"] for r in remaining], ["T1"])

    def test_consecutive_dark_nights_counts_backward_from_last_measurement(self):
        # Node last delivered a measurement 3 nights ago -> streak of 2
        # (yesterday and the night before had no measurement; the night with
        # a measurement ends the streak).
        three_nights_ago = datetime.now(timezone.utc) - timedelta(days=3)
        self.db.execute(
            "INSERT INTO measurements (node_id, target_name, bjd, magnitude, "
            " uncertainty, received_at) VALUES "
            "('nd_streak','V1',2460500.5,13.0,0.05,%s)",
            (three_nights_ago.isoformat(),))
        self.assertEqual(reflow._consecutive_dark_nights("nd_streak"), 2)

    def test_consecutive_dark_nights_zero_for_node_active_last_night(self):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        self.db.execute(
            "INSERT INTO measurements (node_id, target_name, bjd, magnitude, "
            " uncertainty, received_at) VALUES "
            "('nd_recent','V1',2460500.5,13.0,0.05,%s)",
            (yesterday.isoformat(),))
        self.assertEqual(reflow._consecutive_dark_nights("nd_recent"), 0)

    def test_consecutive_dark_nights_caps_at_lookback_for_never_seen_node(self):
        self.assertEqual(
            reflow._consecutive_dark_nights("nd_never_seen", max_lookback=5), 5)

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

    # ── Graduated auto-cap ───────────────────────────────────────────────────────

    def _reconciled_night(self, night, outcome, n=1):
        for i in range(n):
            self.db.execute(
                "INSERT INTO reflow_log (night, from_node, to_node, target_id, "
                " target_name, expected_info, outcome, created_at) "
                "VALUES (%s,'nd_a','nd_b',%s,'V1',0.4,%s,%s)",
                (night, f"T{i}", outcome, self._now()))

    def test_effective_cap_starts_low_with_no_history(self):
        cfg = {"scheduler": {"reflow_max_per_night": 200,
                             "reflow_grad_start_cap": 5}}
        self.assertEqual(reflow._effective_cap(cfg), 5)

    def test_effective_cap_ignores_history_when_auto_grade_disabled(self):
        cfg = {"scheduler": {"reflow_max_per_night": 200,
                             "reflow_auto_grade": False}}
        self.assertEqual(reflow._effective_cap(cfg), 200)

    def test_effective_cap_escalates_after_clean_nights(self):
        cfg = {"scheduler": {"reflow_max_per_night": 200,
                             "reflow_grad_start_cap": 5,
                             "reflow_grad_min_nights": 3,
                             "reflow_grad_min_delivery_rate": 0.7,
                             "reflow_grad_step_factor": 2.0}}
        night = datetime.now(timezone.utc)
        for i in range(3):
            self._reconciled_night(
                (night - timedelta(days=i + 1)).strftime("%Y-%m-%d"), "delivered")
        # 3 clean nights == min_nights -> one escalation step: 5 * 2.0 = 10.
        self.assertEqual(reflow._effective_cap(cfg), 10)

    def test_effective_cap_holds_at_ceiling(self):
        cfg = {"scheduler": {"reflow_max_per_night": 12,
                             "reflow_grad_start_cap": 5,
                             "reflow_grad_min_nights": 3,
                             "reflow_grad_min_delivery_rate": 0.7,
                             "reflow_grad_step_factor": 2.0}}
        night = datetime.now(timezone.utc)
        for i in range(9):
            self._reconciled_night(
                (night - timedelta(days=i + 1)).strftime("%Y-%m-%d"), "delivered")
        # Many escalation steps would blow past 200, but the ceiling caps it.
        self.assertEqual(reflow._effective_cap(cfg), 12)

    def test_effective_cap_drops_back_on_poor_delivery_rate(self):
        cfg = {"scheduler": {"reflow_max_per_night": 200,
                             "reflow_grad_start_cap": 5,
                             "reflow_grad_min_nights": 3,
                             "reflow_grad_min_delivery_rate": 0.7}}
        night = datetime.now(timezone.utc)
        for i in range(4):
            self._reconciled_night(
                (night - timedelta(days=i + 1)).strftime("%Y-%m-%d"), "missed")
        self.assertEqual(reflow._effective_cap(cfg), 5)

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

    # ── Work-starved detection + top-up dispatch ────────────────────────────────

    def _starved_node(self, node_id):
        from cloud import live
        self.db.execute(
            """INSERT INTO nodes (node_id, api_key, latitude, longitude,
                   registered_at, last_heartbeat, status)
               VALUES (%s,'k',40,0,%s,%s,'active')""",
            (node_id, self._now(), self._now()))
        live.record_state(node_id,
                          {"phase": "idle", "is_dark": True,
                           "detail": {"work_starved": True}},
                          heartbeat_s=5)

    def test_detect_starved_requires_explicit_flag(self):
        from cloud import live
        self._starved_node("nd_starved")
        # Idle + dark but no flag: NOT starved (idle races between plan items).
        self.db.execute(
            """INSERT INTO nodes (node_id, api_key, latitude, longitude,
                   registered_at, last_heartbeat, status)
               VALUES ('nd_idle','k',40,0,%s,%s,'active')""",
            (self._now(), self._now()))
        live.record_state("nd_idle", {"phase": "idle", "is_dark": True},
                          heartbeat_s=5)
        # Clouded node with the flag set: not a candidate either.
        self.db.execute(
            """INSERT INTO nodes (node_id, api_key, latitude, longitude,
                   registered_at, last_heartbeat, status)
               VALUES ('nd_cloud','k',40,0,%s,%s,'active')""",
            (self._now(), self._now()))
        live.record_state("nd_cloud",
                          {"phase": "clouded", "is_dark": True,
                           "detail": {"work_starved": True}}, heartbeat_s=5)
        starved = reflow.detect_starved({})
        self.assertEqual([s["node_id"] for s in starved], ["nd_starved"])

    def test_detect_starved_skips_node_with_pending_topup(self):
        self._starved_node("nd_pending")
        expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.db.execute(
            """INSERT INTO interrupts (target_id, name, ra_deg, dec_deg,
                   reason, node_ids, created_at, expires_at)
               VALUES ('T1','V1',10,20,'topup',%s,%s,%s)""",
            (json.dumps(["nd_pending"]), self._now(), expires))
        self.assertEqual(reflow.detect_starved({}), [])

    def test_topup_dispatch_writes_reason_and_counts_against_cap(self):
        before = reflow._reflows_tonight()
        placements = [{"to_node": "nd_starved", "target_id": "T9",
                       "target_name": "V9", "ra_deg": 10.0, "dec_deg": 20.0,
                       "mag": 12.0, "expected_info": 0.2}]
        n = reflow.dispatch_reflow("nd_starved", placements, {},
                                   reason="topup")
        self.assertEqual(n, 1)
        row = self.db.query_one(
            "SELECT reason, node_ids FROM interrupts WHERE target_id='T9'")
        self.assertEqual(row["reason"], "topup")
        self.assertIn("nd_starved", self.db.loads(row["node_ids"], []))
        log = self.db.query_one(
            "SELECT reason, from_node, to_node FROM reflow_log "
            "WHERE target_id='T9'")
        self.assertEqual(log["reason"], "topup")
        self.assertEqual(log["from_node"], "nd_starved")
        self.assertEqual(log["to_node"], "nd_starved")
        # Top-ups share the graduated nightly cap with dropout reflows.
        self.assertEqual(reflow._reflows_tonight(), before + 1)
        # And the dispatch published a push signal to the right node.
        ev = self.db.query_one(
            "SELECT node_id, kind FROM dispatch_events "
            "ORDER BY id DESC LIMIT 1")
        self.assertEqual(ev["node_id"], "nd_starved")
        self.assertEqual(ev["kind"], "interrupt")

    def test_dropout_dispatch_keeps_legacy_reason(self):
        placements = [{"to_node": "nd_b", "target_id": "T8",
                       "target_name": "V8", "ra_deg": 10.0, "dec_deg": 20.0,
                       "mag": None, "expected_info": 0.3}]
        reflow.dispatch_reflow("nd_a", placements, {})
        row = self.db.query_one(
            "SELECT reason FROM interrupts WHERE target_id='T8'")
        self.assertEqual(row["reason"], "reflow")
        log = self.db.query_one(
            "SELECT reason FROM reflow_log WHERE target_id='T8'")
        self.assertEqual(log["reason"], "dropout")


if __name__ == "__main__":
    unittest.main()
