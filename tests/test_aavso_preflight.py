"""AAVSO research nights must not start dry-run or without credentials.

Starfront 2026-09-05 (issue #65): a research night ran with aavso.dry_run true
and unset observer credentials. submit() already skips at POST time; these
tests pin the earlier gate so tonight_accept / schedule start refuse first and
name the missing config keys.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pytest

from src.aavso_submission import preflight
from telescope_mcp.client import AgentClient, CloudClient
from telescope_mcp.local_server import build_server


def _cfg(**aavso) -> dict:
    base = {
        "aavso": {
            "observer_code": "EGBA",
            "username": "owner",
            "password": "secret",
            "dry_run": False,
            "submit_from_node": True,
        },
        "cloud": {"enabled": False},
    }
    base["aavso"].update(aavso)
    return base


# ── unit: preflight() ─────────────────────────────────────────────────────────

def test_ready_config_passes():
    pf = preflight(_cfg())
    assert pf["ok"] is True
    assert pf["problems"] == []
    assert pf["dry_run"] is False
    assert pf["observer_code_set"] is True
    assert pf["credentials_set"] is True


def test_dry_run_true_is_named():
    pf = preflight(_cfg(dry_run=True))
    assert pf["ok"] is False
    assert "aavso.dry_run: true" in pf["problems"]
    assert "aavso.dry_run" in pf["message"]
    assert "config.yaml" in pf["message"]


def test_missing_observer_code_is_named():
    pf = preflight(_cfg(observer_code=""))
    assert pf["ok"] is False
    assert "aavso.observer_code" in pf["problems"]
    assert "aavso.observer_code" in pf["message"]


def test_missing_credentials_named_when_node_posts():
    pf = preflight(_cfg(username="", password=""))
    assert pf["ok"] is False
    assert "aavso.username/password" in pf["problems"]


def test_credentials_optional_when_cloud_owns_submission():
    """Cloud batching uses the network OBSCODE; node need not POST."""
    pf = preflight(_cfg(
        username="", password="",
        submit_from_node=False,
    ))
    assert pf["ok"] is True
    assert pf["credentials_set"] is False
    assert "aavso.username/password" not in pf["problems"]


def test_template_defaults_refuse_research():
    """build/config.template.yaml ships dry_run true and empty identity."""
    pf = preflight({
        "aavso": {
            "observer_code": "",
            "username": "",
            "password": "",
            "dry_run": True,
        },
        "cloud": {"enabled": True},
    })
    assert pf["ok"] is False
    assert "aavso.dry_run: true" in pf["problems"]
    assert "aavso.observer_code" in pf["problems"]
    # cloud.enabled => submit_from_node defaults False, so creds not required
    assert "aavso.username/password" not in pf["problems"]
    assert "dry_run: false" in pf["message"]


def test_how_to_set_mentions_env_vars():
    pf = preflight(_cfg(dry_run=True, observer_code=""))
    assert "AAVSO_USERNAME" in pf["message"]
    assert "AAVSO_PASSWORD" in pf["message"]


# ── MCP: tonight_accept refuses before cloud POST ─────────────────────────────

def _call(server, name, args=None):
    import asyncio
    result = asyncio.run(server.call_tool(name, args or {}))
    text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []))
    return text, result


class TonightAcceptAavsoPreflightTest(unittest.TestCase):

    def setUp(self):
        self.agent = MagicMock(spec=AgentClient)
        self.client = MagicMock(spec=CloudClient)
        self.client.base = "https://example.invalid"
        self.client.authenticated = True
        self.agent.get.side_effect = self._agent_get
        self.client.post.return_value = {
            "node_id": "node_600334db", "status": "accepted",
            "observing": True, "proposal": {"research_hours": 4},
        }
        self._aavso = {
            "ok": False,
            "dry_run": True,
            "observer_code_set": False,
            "credentials_set": False,
            "problems": ["aavso.dry_run: true", "aavso.observer_code"],
            "message": (
                "AAVSO research blocked — aavso.dry_run: true; "
                "aavso.observer_code. Set aavso.observer_code in config.yaml."
            ),
            "last_submission": None,
        }
        self.server = build_server(self.agent, self.client)

    def _agent_get(self, path, *args, **kwargs):
        if path == "/api/cloud/identity":
            return {"registered": True, "node_id": "node_600334db"}
        if path == "/api/aavso":
            return dict(self._aavso)
        raise AssertionError(f"unexpected agent GET {path}")

    def test_tonight_accept_refuses_and_names_problems(self):
        text, _ = _call(self.server, "tonight_accept", {})
        self.client.post.assert_not_called()
        self.assertIn("aavso.dry_run", text)
        self.assertIn("aavso.observer_code", text)
        self.assertIn("accepted", text.lower())

    def test_tonight_accept_proceeds_when_ready(self):
        self._aavso = {
            "ok": True, "dry_run": False, "problems": [],
            "message": "AAVSO submission ready.",
            "last_submission": None,
        }
        _call(self.server, "tonight_accept", {})
        self.client.post.assert_called_once()
        path = self.client.post.call_args[0][0]
        self.assertIn("node_600334db", path)

    def test_scheduled_night_accept_also_refuses(self):
        text, _ = _call(self.server, "scheduled_night_accept", {
            "node_id": "node_600334db", "date": "2026-09-10",
        })
        self.client.post.assert_not_called()
        self.assertIn("aavso.dry_run", text)


    def test_tonight_accept_fails_closed_on_api_error(self):
        """ApiError from /api/aavso must refuse, not wave through (Codex P1)."""
        from telescope_mcp.client import ApiError

        def boom(path, *args, **kwargs):
            if path == "/api/cloud/identity":
                return {"registered": True, "node_id": "node_600334db"}
            if path == "/api/aavso":
                raise ApiError(503, "agent unreachable")
            raise AssertionError(f"unexpected agent GET {path}")

        self.agent.get.side_effect = boom
        text, _ = _call(self.server, "tonight_accept", {})
        self.client.post.assert_not_called()
        self.assertIn("could not verify", text.lower())
        self.assertIn("aavso", text.lower())

    def test_scheduled_night_accept_skips_local_preflight_for_sibling(self):
        """Local /api/aavso only gates this computer's node (Codex P2)."""
        text, _ = _call(self.server, "scheduled_night_accept", {
            "node_id": "node_sibling", "date": "2026-09-10",
        })
        self.client.post.assert_called_once()
        path = self.client.post.call_args[0][0]
        self.assertIn("node_sibling", path)
        # Local node is unready in setUp; sibling must not inherit that refuse.
        self.assertNotIn("aavso.dry_run", text)


# ── node: schedule start gate ─────────────────────────────────────────────────

class CloudPlanRearmTest(unittest.TestCase):
    """Blocked AAVSO plans must be re-deliverable after config is fixed."""

    def setUp(self):
        import src.dashboard as dash
        import src.telemetry as telemetry
        self.dash = dash
        telemetry.reset_for_tests()
        self._orig_cloud = dash._cloud
        with dash._sched_lock:
            dash._sched_state.update(running=False, cancelled=False,
                                     error=None, current_phase="")

    def tearDown(self):
        import src.telemetry as telemetry
        self.dash._cloud = self._orig_cloud
        with self.dash._sched_lock:
            self.dash._sched_state.update(running=False, cancelled=False)
        telemetry.reset_for_tests()

    def test_blocked_cloud_plan_rearms_last_plan_id(self):
        cloud = MagicMock()
        cloud._last_plan_id = "plan_abc"
        cloud.status = {"plan_pending_review": False}
        cloud.rearm_plan_delivery = MagicMock(
            side_effect=lambda: setattr(cloud, "_last_plan_id", None))
        self.dash._cloud = cloud
        with patch.object(self.dash, "_load_config", return_value=_cfg(dry_run=True)):
            self.dash._on_cloud_plan(
                [{"target": "Z Cam", "ra": 8.0, "dec": 73.0, "mag": 10.0}])
        cloud.rearm_plan_delivery.assert_called_once()
        self.assertIsNone(cloud._last_plan_id)
        self.assertTrue(cloud.status.get("aavso_blocked"))
        with self.dash._sched_lock:
            self.assertEqual(self.dash._sched_state["current_phase"], "blocked_aavso")



class ScheduleAavsoPreflightTest(unittest.TestCase):

    def setUp(self):
        import src.dashboard as dash
        self.dash = dash
        with dash._sched_lock:
            dash._sched_state["running"] = False
            dash._sched_state["cancelled"] = False
            dash._sched_state["error"] = None
            dash._sched_state["current_phase"] = ""

    def test_manual_schedule_blocked_when_dry_run(self):
        with patch.object(self.dash, "_load_config", return_value=_cfg(dry_run=True)):
            reason = self.dash._aavso_research_block_reason("manual")
        self.assertIsNotNone(reason)
        self.assertIn("aavso.dry_run", reason)

    def test_interrupt_not_blocked(self):
        with patch.object(self.dash, "_load_config", return_value=_cfg(dry_run=True)):
            reason = self.dash._aavso_research_block_reason("interrupt")
        self.assertIsNone(reason)

    def test_run_schedule_bg_sets_blocked_phase(self):
        with patch.object(self.dash, "_load_config", return_value=_cfg(dry_run=True)):
            self.dash._run_schedule_bg(
                [{"target": "Z Cam", "ra": 8.0, "dec": 73.0,
                  "expDur": 60, "expCount": 1}],
                source="cloud",
            )
        with self.dash._sched_lock:
            self.assertFalse(self.dash._sched_state["running"])
            self.assertEqual(self.dash._sched_state["current_phase"], "blocked_aavso")
            self.assertIn("aavso.dry_run", self.dash._sched_state["error"] or "")


if __name__ == "__main__":
    unittest.main()
