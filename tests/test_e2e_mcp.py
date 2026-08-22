#!/usr/bin/env python3
"""End-to-end: a real member journey, over the real protocol, against a real database.

Every other test here mocks one side or the other. This one does not:

  * PostgreSQL     an ephemeral cluster, real schema, real migrations
  * the cloud      cloud/server.py itself, on a real socket
  * the protocol   a real MCP client, spawning the server as a subprocess and
                   speaking JSON-RPC over its stdin/stdout
  * the tools      dispatched by the SDK, not called as Python functions

So it exercises the seams the unit tests cannot: that the schema actually
creates, that tool arguments survive JSON round-tripping, that the stdio
transport is not corrupted by a stray print, and that the endpoints exist at
the paths the tools ask for. A typo in a URL passes every mocked test and fails
here.

Slow (a Postgres boot), so it is marked and skipped when PG is unavailable.

Run with:  python3 -m pytest tests/test_e2e_mcp.py -v
"""

import json
import os
import shutil
import sys
import threading
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pg_available() -> bool:
    if shutil.which("initdb"):
        return True
    import glob
    return bool(glob.glob("/opt/homebrew/opt/postgresql*/bin/initdb"))


@unittest.skipUnless(_pg_available(), "PostgreSQL not installed")
class EndToEndMcpTest(unittest.TestCase):
    """One member, one telescope, one night's decisions — through the real stack."""

    ADMIN_KEY = "e2e-admin-key"

    @classmethod
    def setUpClass(cls):
        from tests.fuzz.pgtmp import TempPostgres
        from werkzeug.serving import make_server

        cls._pg = TempPostgres().__enter__()
        os.environ["DATABASE_URL"] = cls._pg.dsn

        from cloud import db
        db.init(cls._pg.dsn)

        import cloud.server as server
        app = server.create_app({"server": {"admin_key": cls.ADMIN_KEY}})

        # The forecast is the one thing that would reach the real internet.
        # Pin it clear so "accepted means observing" is a statement about the
        # intent logic rather than about tonight's weather in Texas.
        cls._weather = patch("cloud.conditions.fetch_astronomy_weather",
                             return_value={"cloud_cover": 0.05, "precipitation": 0.0})
        cls._weather.start()

        cls._server = make_server("127.0.0.1", 0, app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls._server.server_port}"
        cls._thread = threading.Thread(target=cls._server.serve_forever,
                                       daemon=True, name="e2e-cloud")
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls._weather.stop()
        cls._server.shutdown()
        cls._pg.__exit__(None, None, None)

    # ── the real protocol ─────────────────────────────────────────────────

    def _session(self, admin: bool = False):
        """A real MCP client talking to the server as a subprocess."""
        import asyncio
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = {**os.environ,
               "TELESCOPE_MCP_CLOUD_BASE": self.base,
               "TELESCOPE_MCP_ENV": "sim"}
        if admin:
            env["TELESCOPE_MCP_ADMIN_KEY"] = self.ADMIN_KEY
        else:
            env.pop("TELESCOPE_MCP_ADMIN_KEY", None)

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "telescope_mcp.cloud_server"],
            cwd=REPO, env=env)
        return params, asyncio, ClientSession, stdio_client

    def _run(self, body, admin: bool = False):
        """Drive `body(call)` inside a live MCP session; returns its result."""
        params, asyncio, ClientSession, stdio_client = self._session(admin)

        async def main():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(name, args=None):
                        res = await session.call_tool(name, args or {})
                        text = "".join(getattr(c, "text", "") for c in res.content)
                        failed = getattr(res, "is_error", None)
                        if failed is None:
                            failed = getattr(res, "isError", False)
                        try:
                            payload = json.loads(text)
                        except ValueError:
                            payload = text
                        return (not failed), payload

                    return await body(call, session)

        return asyncio.run(main())

    # ── the journey ───────────────────────────────────────────────────────

    def test_the_whole_member_journey(self):
        """Sign up, link a telescope, decide tonight, stop it, resume it."""

        async def journey(call, session):
            steps = {}

            tools = (await session.list_tools()).tools
            steps["tools_advertised"] = len(tools)

            # -- a brand new member -------------------------------------------
            import requests
            reg = requests.post(f"{self.base}/api/v1/auth/register", json={
                "email": "e2e@example.invalid", "password": "correct horse battery",
                "display_name": "E2E Member"}, timeout=10)
            steps["register_status"] = reg.status_code

            ok, me = await call("auth_login", {"email": "e2e@example.invalid",
                                               "password": "correct horse battery"})
            steps["login"] = (ok, me)

            # A session token must never come back in the transcript.
            steps["token_leaked"] = "token" in json.dumps(me).lower()

            # -- link a telescope ---------------------------------------------
            ok, attached = await call("member_attach_node", {
                "location_name": "Starfront, TX",
                "latitude": 30.9, "longitude": -103.9,
                "telescope_display_name": "Starfront Mini",
                "telescope_model": "seestar_s50"})
            steps["attach"] = ok
            node_id = attached.get("node_id") if isinstance(attached, dict) else None
            steps["node_id"] = node_id

            ok, nodes = await call("member_list_nodes")
            steps["my_nodes"] = [n.get("node_id") for n in nodes.get("nodes", [])]

            # -- tonight ------------------------------------------------------
            ok, tonight = await call("tonight", {"node_id": node_id})
            steps["tonight_status"] = tonight.get("status")
            steps["tonight_proposal"] = tonight.get("proposal", {})
            steps["tonight_nudge"] = tonight.get("nudge", "")

            ok, accepted = await call("tonight_accept", {
                "node_id": node_id, "research_hours": 2, "imaging_after": True})
            steps["accepted"] = (accepted.get("status"), accepted.get("observing"))
            steps["accepted_shape"] = accepted.get("proposal", {})

            # -- the override -------------------------------------------------
            ok, down = await call("stand_down", {
                "node_id": node_id, "reason": "mount making a noise"})
            steps["stood_down"] = (down.get("status"), down.get("observing"))
            steps["stand_down_reason"] = down.get("reason", "")

            ok, still = await call("tonight", {"node_id": node_id})
            steps["after_stand_down"] = still.get("observing")

            ok, back = await call("resume", {"node_id": node_id})
            steps["resumed"] = (back.get("status"), back.get("observing"))

            # -- confirmation gate --------------------------------------------
            ok, refused = await call("member_disconnect_node", {"node_id": node_id})
            steps["unconfirmed_disconnect_refused"] = not ok

            return steps

        s = self._run(journey)

        self.assertGreater(s["tools_advertised"], 70)
        self.assertEqual(s["register_status"], 200, "member registration failed")
        self.assertTrue(s["login"][0], f"login failed: {s['login'][1]}")
        self.assertFalse(s["token_leaked"], "a session token reached the transcript")

        self.assertTrue(s["attach"], "linking a telescope failed")
        self.assertTrue(s["node_id"], "attach returned no node_id")
        self.assertIn(s["node_id"], s["my_nodes"], "the linked node is not listed")

        # A member who has never answered still has a proposal waiting.
        self.assertEqual(s["tonight_status"], "proposed")
        self.assertEqual(s["tonight_proposal"].get("mode"), "research",
                         "the default recommendation must be research")
        self.assertTrue(s["tonight_nudge"], "no nudge was surfaced")

        self.assertEqual(s["accepted"], ("accepted", True))
        self.assertIs(s["after_stand_down"], False)
        self.assertEqual(s["accepted_shape"].get("research_hours"), 2.0,
                         "the accepted shape did not survive the round trip")
        self.assertTrue(s["accepted_shape"].get("imaging_after"))

        self.assertEqual(s["stood_down"][0], "stood_down")
        self.assertIs(s["stood_down"][1], False,
                      "stand_down must report observing=False explicitly — a "
                      "missing field would let a caller believe it stopped when "
                      "the reply says nothing either way")
        self.assertIn("mount making a noise", s["stand_down_reason"])

        self.assertEqual(s["resumed"], ("accepted", True), "resume did not restore")

        self.assertTrue(s["unconfirmed_disconnect_refused"],
                        "an unconfirmed disconnect was allowed")

    def test_fleet_integrity_runs_against_the_real_database(self):
        """The check must survive a real schema, not just a mocked one."""

        async def sweep(call, session):
            ok, result = await call("fleet_integrity_check")
            return ok, result

        ok, result = self._run(sweep, admin=True)
        self.assertTrue(ok, f"integrity check failed: {result}")
        self.assertIn("checks_run", result)
        self.assertEqual(len(result["checks_run"]), 7)
        self.assertEqual(result["errors"], [],
                         f"a check failed against the real schema: {result['errors']}")

    def test_integrity_is_refused_without_an_admin_key(self):
        async def sweep(call, session):
            return await call("fleet_integrity_check")

        ok, result = self._run(sweep, admin=False)
        self.assertFalse(ok, "the integrity check ran without an admin key")

    def test_the_patrol_reports_against_the_real_database(self):
        from telescope_mcp import patrol
        from telescope_mcp.client import CloudClient
        client = CloudClient(base=self.base)
        client._admin_key = self.ADMIN_KEY
        result = patrol.run(client)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIn("report", result)
        self.assertEqual(result["check_errors"], [])


if __name__ == "__main__":
    unittest.main()
