#!/usr/bin/env python3
"""The MCP safety rails must actually refuse, not merely document a refusal.

Three rails, each of which fails open if it is wrong:

  environment gating   a tool that moves a mount must refuse production
  confirmation         an irreversible tool must refuse without confirm=true
  secret redaction     no tool may return a node api_key or session token

The third matters most: an api_key echoed into a chat transcript is a
credential leak that no later fix can recall.

Run with:  python3 -m pytest tests/test_mcp_guards.py
"""

import unittest
from unittest.mock import MagicMock, patch

from telescope_mcp import guard
from telescope_mcp.client import AgentClient, ApiError, CloudClient
from telescope_mcp.cloud_server import build_server as build_cloud
from telescope_mcp.local_server import build_server as build_local


def call(server, name: str, args: dict | None = None) -> tuple[bool, str]:
    """Invoke a tool, returning (ok, text).

    MCPServer.call_tool raises ToolError for a failing tool body; the protocol
    layer turns that into CallToolResult(isError=True). Tests care about the
    observable outcome either way, so both paths collapse to the same pair.
    """
    import asyncio
    from mcp.server.mcpserver.exceptions import ToolError
    try:
        result = asyncio.run(server.call_tool(name, args or {}))
    except ToolError as exc:
        return False, str(exc)
    text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []))
    # The SDK spells this is_error; tolerate the wire spelling too so a rename
    # cannot silently turn every failure assertion into a pass.
    failed = getattr(result, "is_error", None)
    if failed is None:
        failed = getattr(result, "isError", False)
    return not failed, text


class EnvironmentGateTest(unittest.TestCase):

    def test_defaults_to_sim_when_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(guard.environment(), "sim")

    def test_unknown_environment_falls_back_to_sim(self):
        """A typo in the env var must not silently mean production."""
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "prd"}, clear=True):
            self.assertEqual(guard.environment(), "sim")

    def test_sim_and_staging_allow_hardware_actions(self):
        for env in ("sim", "staging"):
            with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": env}, clear=True):
                guard.require_non_production("slew")  # must not raise

    def test_production_refuses_hardware_actions(self):
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "production"}, clear=True):
            with self.assertRaises(guard.GuardError) as ctx:
                guard.require_non_production("slew the telescope")
        self.assertIn("slew the telescope", str(ctx.exception))
        self.assertIn("TELESCOPE_MCP_ENV", str(ctx.exception))

    def test_production_override_is_explicit_and_works(self):
        with patch.dict("os.environ", {
            "TELESCOPE_MCP_ENV": "production",
            "TELESCOPE_MCP_ALLOW_PRODUCTION_WRITES": "1",
        }, clear=True):
            guard.require_non_production("slew")  # must not raise

    def test_override_ignores_non_affirmative_values(self):
        for value in ("0", "false", "no", "", "maybe"):
            with patch.dict("os.environ", {
                "TELESCOPE_MCP_ENV": "production",
                "TELESCOPE_MCP_ALLOW_PRODUCTION_WRITES": value,
            }, clear=True):
                with self.assertRaises(guard.GuardError):
                    guard.require_non_production("slew")


class ConfirmationTest(unittest.TestCase):

    def test_missing_confirmation_raises(self):
        with self.assertRaises(guard.GuardError) as ctx:
            guard.require_confirmation(False, "delete this member account")
        self.assertIn("delete this member account", str(ctx.exception))

    def test_confirmation_passes(self):
        guard.require_confirmation(True, "delete this member account")


class UntrustedWrappingTest(unittest.TestCase):

    def test_wraps_with_provenance_and_source(self):
        wrapped = guard.untrusted({"lines": ["boom"]}, "node agent log file")
        self.assertEqual(wrapped["_provenance"], "untrusted")
        self.assertEqual(wrapped["_source"], "node agent log file")
        self.assertEqual(wrapped["content"], {"lines": ["boom"]})
        self.assertIn("not instructions", wrapped["_note"])


class HardwareToolGatingTest(unittest.TestCase):
    """Tools that move hardware must refuse before issuing any HTTP request."""

    MOVES_HARDWARE = (
        "node_slew", "node_park", "node_unpark", "node_expose", "node_nudge",
        "node_arm_open", "node_autofocus_start", "node_center_start",
        "node_horizon_scan_start", "node_schedule_run", "node_safety_reset",
        "node_set_tracking",
    )

    #: Stopping activity must always be possible, including in production --
    #: a rail that blocks the brake is worse than no rail.
    ALWAYS_ALLOWED = ("node_abort_exposure", "node_arm_close",
                      "node_schedule_abort")

    def setUp(self):
        self.agent = MagicMock(spec=AgentClient)
        self.agent.get.return_value = {}
        self.agent.post.return_value = {}
        self.agent.delete.return_value = {}
        self.server = build_local(self.agent, with_cloud=False)

    def _args(self, name):
        return {
            "node_slew": {"ra_hours": 5.5, "dec_deg": 22.0},
            "node_center_start": {"ra_hours": 5.5, "dec_deg": 22.0},
            "node_expose": {"seconds": 1.0},
            "node_nudge": {"direction": "north"},
            "node_set_tracking": {"enabled": True},
        }.get(name, {})

    def test_hardware_tools_refuse_in_production_without_calling_the_agent(self):
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "production"}, clear=True):
            for name in self.MOVES_HARDWARE:
                self.agent.reset_mock()
                ok, text = call(self.server, name, self._args(name))
                self.assertFalse(ok, f"{name} did not refuse against production")
                self.assertIn("production", text)
                self.agent.post.assert_not_called()
                self.agent.delete.assert_not_called()

    def test_stopping_activity_is_allowed_in_production(self):
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "production"}, clear=True):
            for name in self.ALWAYS_ALLOWED:
                ok, text = call(self.server, name, {})
                self.assertTrue(ok, f"{name} was blocked ({text}); it stops "
                                    f"activity and must always be available")

    def test_hardware_tools_run_in_sim(self):
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "sim"}, clear=True):
            ok, text = call(self.server, "node_slew",
                            {"ra_hours": 5.5, "dec_deg": 22.0})
            self.assertTrue(ok, text)
            self.agent.post.assert_called_once()


class SecretRedactionTest(unittest.TestCase):

    def test_node_identity_never_returns_the_api_key(self):
        agent = MagicMock(spec=AgentClient)
        agent.get.return_value = {
            "registered": True, "node_id": "node_abc",
            "api_key": "SUPER-SECRET-KEY-VALUE",
        }
        server = build_local(agent, with_cloud=False)
        _, text = call(server, "node_identity", {})
        self.assertIn("node_abc", text)
        self.assertNotIn("SUPER-SECRET-KEY-VALUE", text)
        self.assertIn("has_api_key", text)

    def test_node_cloud_state_strips_the_api_key(self):
        agent = MagicMock(spec=AgentClient)
        agent.get.return_value = {
            "registered": True, "node_id": "node_abc",
            "pair_token": "ABC123", "api_key": "SUPER-SECRET-KEY-VALUE",
        }
        server = build_local(agent, with_cloud=False)
        _, text = call(server, "node_cloud_state", {})
        self.assertNotIn("SUPER-SECRET-KEY-VALUE", text)

    def test_auth_login_does_not_echo_the_session_token(self):
        client = MagicMock(spec=CloudClient)
        client.base = "https://example.invalid"
        client.post.return_value = {"token": "SECRET-SESSION-TOKEN",
                                    "user_id": "user_1"}
        server = build_cloud(client)
        _, text = call(server, "auth_login",
                       {"email": "a@b.c", "password": "hunter2"})
        self.assertNotIn("SECRET-SESSION-TOKEN", text)
        self.assertIn("user_1", text)
        client.set_token.assert_called_once_with("SECRET-SESSION-TOKEN")


class ErrorSurfaceTest(unittest.TestCase):

    def test_api_errors_reach_the_caller_as_readable_text(self):
        client = MagicMock(spec=CloudClient)
        client.base = "https://example.invalid"
        client.get.side_effect = ApiError(404, "No node with that id.")
        server = build_cloud(client)
        ok, text = call(server, "member_node_live", {"node_id": "nope"})
        self.assertFalse(ok)
        self.assertIn("No node with that id.", text)


if __name__ == "__main__":
    unittest.main()
