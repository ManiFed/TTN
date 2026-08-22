#!/usr/bin/env python3
"""Standalone mode: a telescope run by chat, with no network account.

`--local` is for someone who wants to drive their own rig and has not joined
the network. The promise is narrow and worth keeping precisely: nothing is
uploaded, nothing is shared, and nothing asks them to sign in to something they
deliberately did not join.

The failure to design against is a tool that quietly reaches the cloud anyway.
So this asserts by *construction* -- no registered tool may hold a CloudClient --
rather than by checking names, which would miss a renamed one.

Run with:  python3 -m pytest tests/test_standalone_mode.py
"""

import asyncio
import unittest
from unittest.mock import MagicMock

from telescope_mcp.client import AgentClient, ApiError, CloudClient
from telescope_mcp.local_server import build_server


def _tools(server):
    return {t.name: t for t in asyncio.run(server.list_tools())}


class ToolSurfaceTest(unittest.TestCase):

    def setUp(self):
        self.agent = MagicMock(spec=AgentClient)
        self.solo = _tools(build_server(self.agent, with_cloud=False))
        self.full = _tools(build_server(self.agent, MagicMock(spec=CloudClient)))

    def test_standalone_shares_the_hardware_tools_and_adds_only_its_own_connect(self):
        """Not a strict subset: standalone swaps connect_my_telescope (which
        needs an account) for connect_telescope (which does not). Everything
        else it has, the networked server has too."""
        extra = set(self.solo) - set(self.full)
        self.assertEqual(extra, {"connect_telescope"})
        self.assertIn("connect_my_telescope", self.full)
        self.assertNotIn("connect_telescope", self.full)

    def test_standalone_still_covers_the_telescope(self):
        """Narrower must not mean crippled."""
        for name in ("connect_telescope", "node_status", "node_slew", "node_park",
                     "node_expose", "node_safety", "node_logs", "diagnose_local",
                     "run_imaging_program", "stacked_preview", "last_image"):
            if name == "diagnose_local":
                continue      # diagnose is cloud-aware; covered separately
            self.assertIn(name, self.solo, f"{name} missing from standalone")
        self.assertGreater(len(self.solo), 40)

    def test_no_standalone_tool_can_reach_the_cloud(self):
        """Asserted by construction, not by name.

        A tool that closes over a CloudClient can talk to the network however it
        is named, so checking names would miss exactly the mistake that matters.
        """
        offenders = []
        for name, tool in self.solo.items():
            fn = getattr(tool, "fn", None) or getattr(tool, "func", None)
            closure = getattr(fn, "__closure__", None) or ()
            for cell in closure:
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(value, CloudClient) or (
                        isinstance(value, MagicMock)
                        and getattr(value, "_spec_class", None) is CloudClient):
                    offenders.append(name)
                    break
        self.assertEqual(offenders, [],
                         f"these standalone tools hold a cloud client: {offenders}")

    def test_account_tools_are_absent(self):
        for name in ("auth_login", "member_list_nodes", "member_attach_node",
                     "tonight", "tonight_accept", "stand_down",
                     "fleet_integrity_check", "admin_start_dry_run",
                     "connect_my_telescope"):
            self.assertNotIn(name, self.solo, f"{name} leaked into standalone")

    def test_tonight_results_stays_because_it_is_node_local(self):
        """It reads the node's own pipeline, not the network -- despite the name
        sitting next to the account-facing tonight_* tools."""
        self.assertIn("tonight_results", self.solo)


class InstructionsTest(unittest.TestCase):

    def test_standalone_never_tells_you_to_sign_in(self):
        server = build_server(MagicMock(spec=AgentClient), with_cloud=False)
        text = server.instructions or ""
        self.assertNotIn("auth_login", text)
        self.assertIn("standalone", text.lower())
        self.assertIn("nothing is uploaded", text.lower())

    def test_networked_instructions_still_mention_the_account(self):
        server = build_server(MagicMock(spec=AgentClient), MagicMock(spec=CloudClient))
        self.assertIn("network", (server.instructions or "").lower())


class ConnectTelescopeTest(unittest.TestCase):
    """The standalone connect: discover, connect, verify. No credentials."""

    def setUp(self):
        self.agent = MagicMock(spec=AgentClient)
        self.discovered = {"servers": [{"host": "192.168.1.44", "port": 11111}]}
        self.status = {"telescope": {"connected": True}}
        self.agent.post.side_effect = lambda p, b=None, timeout=15.0: (
            self.discovered if p == "/api/discover" else {})
        self.agent.get.side_effect = lambda p, q=None, timeout=8.0: self.status
        self.server = build_server(self.agent, with_cloud=False)

    def _call(self, name, args=None):
        from mcp.server.mcpserver.exceptions import ToolError
        try:
            res = asyncio.run(self.server.call_tool(name, args or {}))
        except ToolError as exc:
            return False, str(exc)
        text = "".join(getattr(c, "text", "") for c in getattr(res, "content", []))
        failed = getattr(res, "is_error", None)
        if failed is None:
            failed = getattr(res, "isError", False)
        return not failed, text

    def test_it_discovers_connects_and_verifies(self):
        ok, text = self._call("connect_telescope")
        self.assertTrue(ok, text)
        self.assertIn('"connected": true', text.lower())
        paths = [c[0][0] for c in self.agent.post.call_args_list]
        self.assertEqual(paths, ["/api/discover", "/api/connect"])

    def test_it_never_installs_credentials(self):
        """The step that needs an account must simply not happen."""
        self._call("connect_telescope")
        paths = [c[0][0] for c in self.agent.post.call_args_list]
        self.assertNotIn("/api/cloud/credentials", paths)

    def test_an_explicit_address_skips_discovery(self):
        self._call("connect_telescope", {"host": "10.0.0.5", "port": 11111})
        paths = [c[0][0] for c in self.agent.post.call_args_list]
        self.assertNotIn("/api/discover", paths)

    def test_nothing_found_still_leads_with_access_point_mode(self):
        self.discovered = {"servers": []}
        ok, text = self._call("connect_telescope")
        self.assertIn("Access Point", text)
        self.assertIn("Station Mode", text)

    def test_a_telescope_that_never_comes_up_is_reported_honestly(self):
        from unittest.mock import patch
        self.status = {"telescope": {"connected": False}}
        with patch("telescope_mcp.tools.setup.time.sleep"):
            ok, text = self._call("connect_telescope")
        self.assertIn('"connected": false', text.lower())
        self.assertIn("node_logs", text)


class CommissioningTest(unittest.TestCase):
    """A standalone node must be able to finish commissioning.

    It used to sit at 'waiting_for_signup' for ever, because the gate asked
    whether the node was linked to a member and a standalone node never is --
    so the thing that tells an owner their telescope is ready never would.
    """

    def _commissioning(self, cloud_enabled: bool, tmpdir):
        from src.commissioning import CommissioningManager
        return CommissioningManager(
            load_config=lambda: {
                "cloud": {"enabled": cloud_enabled},
                "safety": {"observer": {"latitude": 31.5, "longitude": -99.2}},
                "photometry": {"solver": "astap", "astap_path": "astap"},
                "image_watcher": {"watch_path": str(tmpdir)},
            },
            is_registered=lambda: False,
            runtime_status=lambda: {},
            telescope_specs=lambda: {},
            state_path=str(tmpdir / "commissioning.json"),
        )

    def test_a_standalone_node_does_not_wait_for_a_signup(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            state = self._commissioning(False, Path(d)).evaluate()
        self.assertNotEqual(state.get("status"), "waiting_for_signup")

    def test_a_networked_node_still_waits_for_one(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            state = self._commissioning(True, Path(d)).evaluate()
        self.assertEqual(state.get("status"), "waiting_for_signup")

    def test_registration_is_not_a_blocking_check_when_standalone(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            state = self._commissioning(False, Path(d)).evaluate()
        check = (state.get("checks") or {}).get("cloud_registration") or {}
        self.assertFalse(check.get("blocking", True),
                         "a standalone node cannot be blocked on linking to an "
                         "account it has deliberately not created")


if __name__ == "__main__":
    unittest.main()
