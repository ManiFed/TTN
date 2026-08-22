#!/usr/bin/env python3
"""End-to-end, node side: real agent, real telescope protocol, real MCP transport.

The cloud end-to-end proves the member journey. This proves the half that
touches hardware:

  * the node agent   src/dashboard.py itself, on a real socket
  * the telescope    a real ALPACA server (faked devices, real protocol)
  * the transport    a real MCP client spawning the local server as a subprocess

What it catches that mocks cannot: an endpoint path the tool asks for but the
agent does not serve, a response shape the tool misreads, and stdout corruption
in --mcp mode -- which would fail here as an unparseable stream rather than as
a wrong answer, and is invisible to every in-process test.

Run with:  python3 -m pytest tests/test_e2e_node_mcp.py -v
"""

import json
import os
import sys
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CONFIG = """
alpaca:
  api_version: 1
devices:
  telescope: {{enabled: true, device_number: 0}}
  camera: {{enabled: true, device_number: 0}}
  focuser: {{enabled: false}}
  covercalibrator: {{enabled: false}}
safety:
  enabled: true
  heartbeat_interval: 0.2
  heartbeat_timeout: 1.0
  disconnect_timeout: 30.0
  reconnect_attempts: 1
  reconnect_delay: 0.5
  park_at_dawn: false
  observer: {{latitude: 31.5, longitude: -99.2}}
observatory:
  latitude: 31.5
  longitude: -99.2
  elevation: 500
  telescope: ZWO Seestar S50
cloud:
  enabled: false
photometry: {{enabled: false}}
image_watcher: {{enabled: false}}
commissioning: {{enabled: false}}
pier_cam: {{enabled: false}}
logging: {{level: WARNING}}
"""


class EndToEndNodeMcpTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path
        from tests.fuzz.fakealpaca import FakeObservatory
        from tests.fuzz.faults import FaultPlan
        from tests.fuzz.harness import _free_port

        cls._orig_cwd = os.getcwd()
        cls.workdir = Path(tempfile.mkdtemp(prefix="e2e_node_"))
        (cls.workdir / "config.yaml").write_text(CONFIG.format())
        os.chdir(cls.workdir)

        # A real ALPACA server, with devices that answer like real ones.
        cls.obs = FakeObservatory(FaultPlan()).start()

        import src.dashboard as dashboard
        cls.dashboard = dashboard
        port = _free_port()
        cls.agent_base = f"http://127.0.0.1:{port}"
        threading.Thread(target=dashboard.launch, args=(port,),
                         daemon=True, name="e2e-node").start()

        import requests
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if requests.get(f"{cls.agent_base}/api/status", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("node agent never became ready")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.obs.stop()
        except Exception:
            pass
        os.chdir(cls._orig_cwd)

    def _run(self, body):
        """Drive `body(call)` in a live MCP session against the real agent."""
        import asyncio
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "telescope_mcp.local_server"],
            cwd=REPO,
            env={**os.environ,
                 "TELESCOPE_MCP_AGENT_BASE": self.agent_base,
                 "TELESCOPE_MCP_ENV": "sim"})

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

    def test_connect_and_drive_a_telescope_through_mcp(self):
        async def journey(call, session):
            steps = {}
            steps["tools"] = len((await session.list_tools()).tools)

            # The agent is up but no telescope is attached yet.
            ok, before = await call("node_status")
            steps["before_connect"] = (ok, (before.get("telescope") or {}).get("connected"))

            # Connect to the ALPACA server directly (discovery is a UDP
            # broadcast, which a loopback-only fake does not answer).
            ok, conn = await call("node_connect_alpaca",
                                  {"host": "127.0.0.1", "port": self.obs.port})
            steps["connect"] = (ok, conn)

            ok, after = await call("node_status")
            steps["after_connect"] = (after.get("telescope") or {}).get("connected")
            steps["camera_connected"] = (after.get("camera") or {}).get("connected")

            # Hardware motion, for real, against the fake mount.
            ok, slew = await call("node_slew", {"ra_hours": 5.5, "dec_deg": 22.0})
            steps["slew"] = (ok, slew)

            ok, park = await call("node_park")
            steps["park"] = ok

            # Diagnosis must assemble without the cloud.
            ok, diag = await call("diagnose")
            steps["diagnose_ok"] = ok
            steps["diagnose_summary"] = diag.get("summary", "")
            steps["diagnose_has_logs"] = "logs" in diag
            steps["logs_untrusted"] = (diag.get("logs") or {}).get("_provenance")

            # Identity must never carry the key.
            ok, ident = await call("node_identity")
            steps["identity"] = ident

            # The safety verdict is what a member is told when it refuses.
            ok, safety = await call("node_safety")
            steps["safety_ok"] = ok
            return steps

        s = self._run(journey)

        self.assertGreater(s["tools"], 100)
        self.assertTrue(s["before_connect"][0], "node_status failed against a live agent")

        self.assertTrue(s["connect"][0], f"connect failed: {s['connect'][1]}")
        self.assertTrue(s["after_connect"], "telescope did not report connected")
        self.assertTrue(s["camera_connected"], "camera did not report connected")

        self.assertTrue(s["slew"][0], f"slew failed: {s['slew'][1]}")
        self.assertTrue(s["park"], "park failed")

        self.assertTrue(s["diagnose_ok"])
        # This harness runs with cloud.enabled=false, so "not registered" is
        # the correct reading. What must never appear is the transport
        # failure -- diagnose reached the agent perfectly well.
        self.assertNotIn("not reachable", s["diagnose_summary"])
        self.assertIn("not registered with the cloud", s["diagnose_summary"])
        self.assertNotIn("telescope is not connected", s["diagnose_summary"])
        self.assertTrue(s["diagnose_has_logs"])
        self.assertEqual(s["logs_untrusted"], "untrusted",
                         "log text must be labelled as data, not instructions; "
                         "a missing label here also means the fetch failed, "
                         "which is what /api/logs (an endless SSE stream) did")

        # has_api_key (a boolean) is fine and useful; the key itself is not.
        self.assertNotIn("api_key", s["identity"].keys(),
                         "node_identity returned the credential itself")
        self.assertIsInstance(s["identity"].get("has_api_key"), bool)
        self.assertTrue(s["safety_ok"])

    def test_hardware_motion_is_refused_against_production(self):
        """The rail must hold over the real transport, not just in-process."""
        import asyncio
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "telescope_mcp.local_server"],
            cwd=REPO,
            env={**os.environ,
                 "TELESCOPE_MCP_AGENT_BASE": self.agent_base,
                 "TELESCOPE_MCP_ENV": "production"})

        async def main():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool(
                        "node_slew", {"ra_hours": 5.5, "dec_deg": 22.0})
                    text = "".join(getattr(c, "text", "") for c in res.content)
                    failed = getattr(res, "is_error", None)
                    if failed is None:
                        failed = getattr(res, "isError", False)
                    # The session must survive a refusal.
                    alive = len((await session.list_tools()).tools)
                    return failed, text, alive

        failed, text, alive = asyncio.run(main())
        self.assertTrue(failed, "a production slew was allowed over the wire")
        self.assertIn("production", text)
        self.assertGreater(alive, 100, "the session died on a refusal")

    def test_the_packaged_entry_point_speaks_clean_json_rpc(self):
        """`TelescopeNetNode --mcp` is what the installer registers.

        A stray print in that path corrupts the JSON-RPC stream, and the client
        fails with a parse error that points nowhere. Only an out-of-process
        check can see it.
        """
        import subprocess
        request = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "e2e", "version": "1"}},
        })
        proc = subprocess.run(
            [sys.executable, "-m", "src.main_service", "--mcp",
             "--data-dir", str(self.workdir)],
            input=request + "\n", capture_output=True, text=True,
            cwd=REPO, timeout=90)
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        self.assertTrue(first, f"no stdout; stderr was: {proc.stderr[-400:]}")
        payload = json.loads(first)          # raises if anything polluted stdout
        self.assertEqual(payload.get("id"), 1)
        self.assertIn("capabilities", payload.get("result", {}))


if __name__ == "__main__":
    unittest.main()
