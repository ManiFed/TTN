#!/usr/bin/env python3
"""Local schedule refill from cloud after cancel (issue #67)."""

import unittest
from unittest.mock import MagicMock, patch

import src.cloud_communicator as cc_mod
import src.dashboard as dash


class ForceRedeliverTest(unittest.TestCase):
    def test_clears_latch_and_polls(self):
        cloud = cc_mod.CloudCommunicator.__new__(cc_mod.CloudCommunicator)
        cloud._last_plan_id = "plan-abc"
        cloud.status = {"plan_items": 0, "last_plan_id": "plan-abc",
                        "plan_pending_review": False}
        polled = {"n": 0}

        def poll():
            polled["n"] += 1
            cloud._last_plan_id = "plan-abc"
            cloud.status["plan_items"] = 38
            cloud.status["last_plan_id"] = "plan-abc"

        cloud._poll_plan = poll
        out = cloud.force_redeliver_current_plan()
        self.assertTrue(out["ok"])
        self.assertEqual(polled["n"], 1)
        self.assertEqual(out["plan_items"], 38)
        self.assertEqual(out["previous_plan_id"], "plan-abc")


class ScheduleResyncApiTest(unittest.TestCase):
    def setUp(self):
        with dash._sched_lock:
            dash._sched_state.update({
                "running": False, "cancelled": False, "total": 0,
                "current_phase": "", "error": None,
            })

    def test_resync_refuses_when_running(self):
        with dash._sched_lock:
            dash._sched_state["running"] = True
        with patch.object(dash, "_cloud", MagicMock()):
            payload, status = dash._resync_schedule_from_cloud()
        self.assertEqual(status, 409)
        self.assertIn("already running", payload["error"])

    def test_resync_calls_force_redeliver(self):
        cloud = MagicMock()
        cloud.force_redeliver_current_plan.return_value = {
            "ok": True, "plan_id": "p1", "plan_items": 38,
        }
        with patch.object(dash, "_cloud", cloud), \
             patch.object(dash, "_aavso_research_block_reason", return_value=None):
            with dash._sched_lock:
                dash._sched_state["total"] = 38
                dash._sched_state["running"] = True
                dash._sched_state["current_phase"] = "waiting_for_dark"
            # running True would 409 — simulate post-delivery state after callback
            with dash._sched_lock:
                dash._sched_state["running"] = False
            payload, status = dash._resync_schedule_from_cloud()
        cloud.force_redeliver_current_plan.assert_called_once()
        self.assertEqual(status, 200)
        self.assertEqual(payload["plan_items"], 38)

    def test_run_with_empty_body_refills_from_cloud(self):
        cloud = MagicMock()
        cloud.force_redeliver_current_plan.return_value = {
            "ok": True, "plan_id": "p1", "plan_items": 12,
        }
        client = dash.app.test_client()
        with patch.object(dash, "_cloud", cloud), \
             patch.object(dash, "_aavso_research_block_reason", return_value=None):
            with dash._sched_lock:
                dash._sched_state["total"] = 12
                dash._sched_state["running"] = False
            resp = client.post("/api/schedule/run", json={})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("refilled_from_cloud"))
        cloud.force_redeliver_current_plan.assert_called_once()


if __name__ == "__main__":
    unittest.main()
