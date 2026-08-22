#!/usr/bin/env python3
"""What happens when the telescope does something the code did not expect.

The reasonable objection to driving hardware from a chat interface is that it
looks fine on a good night and falls apart on a bad one. Real telescopes fail in
specific, unglamorous ways: the mount reports slewing forever, the camera never
becomes ready, a device drops off the network mid-run, the ALPACA server returns
500s or truncated JSON.

This drives the real MCP surface against a real node agent talking to a real
ALPACA server whose devices are misbehaving on purpose, and asserts the only
things that actually matter when nobody is awake:

  * a tool fails as a readable message, never as a crash
  * one broken device does not take out the tools that do not touch it
  * the session survives, so the next question can still be asked
  * diagnose still answers, because that is what gets reached for when
    something is wrong
  * nothing hangs -- every call returns

It deliberately does not assert that operations succeed. Under these faults
they should not. The claim under test is that failure stays legible and local.

Run with:  python3 -m pytest tests/test_fault_tolerance_mcp.py -v
"""

import asyncio
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
cloud: {{enabled: false}}
photometry: {{enabled: false}}
image_watcher: {{enabled: false}}
commissioning: {{enabled: false}}
pier_cam: {{enabled: false}}
logging: {{level: WARNING}}
"""

#: Every call must come back inside this. A tool that hangs is worse than one
#: that fails: it takes the conversation with it.
CALL_BUDGET_S = 150.0


class _FaultyRig:
    """A real node agent, connected to a real ALPACA server that misbehaves."""

    def __init__(self, profile: str, seed: int = 7):
        import tempfile
        from pathlib import Path
        from tests.fuzz.fakealpaca import FakeObservatory
        from tests.fuzz.faults import FaultPlan
        from tests.fuzz.harness import _free_port

        self.orig_cwd = os.getcwd()
        self.workdir = Path(tempfile.mkdtemp(prefix="fault_mcp_"))
        (self.workdir / "config.yaml").write_text(CONFIG.format())
        os.chdir(self.workdir)

        plan = FaultPlan.generate(seed, scenario_s=600.0, profile=profile)
        self.obs = FakeObservatory(plan).start()

        import src.dashboard as dashboard
        port = _free_port()
        self.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=dashboard.launch, args=(port,),
                         daemon=True, name="fault-node").start()

        import requests
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if requests.get(f"{self.base}/api/status", timeout=2).ok:
                    return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("node agent never became ready")

    def stop(self):
        try:
            self.obs.stop()
        except Exception:
            pass
        os.chdir(self.orig_cwd)


def drive(base: str, calls: list) -> dict:
    """Run `calls` through a real MCP session; return per-call outcomes."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "telescope_mcp.local_server", "--local"],
        cwd=REPO,
        env={**os.environ, "TELESCOPE_MCP_AGENT_BASE": base,
             "TELESCOPE_MCP_ENV": "sim"})

    async def main():
        results = {}
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for name, args in calls:
                    started = time.time()
                    try:
                        res = await asyncio.wait_for(
                            session.call_tool(name, args), timeout=CALL_BUDGET_S)
                        text = "".join(getattr(c, "text", "") for c in res.content)
                        failed = getattr(res, "is_error", None)
                        if failed is None:
                            failed = getattr(res, "isError", False)
                        try:
                            payload = json.loads(text)
                        except ValueError:
                            payload = None
                        results[name] = {"returned": True, "ok": not failed,
                                         # Truncated for readable failures; the
                                         # parsed payload is what assertions
                                         # about content should use.
                                         "text": text[:600],
                                         "payload": payload,
                                         "seconds": time.time() - started}
                    except asyncio.TimeoutError:
                        results[name] = {"returned": False, "ok": False,
                                         "text": "TIMED OUT",
                                         "seconds": time.time() - started}
                    except Exception as exc:
                        results[name] = {"returned": False, "ok": False,
                                         "text": f"{type(exc).__name__}: {exc}",
                                         "seconds": time.time() - started}
                # The session must still be usable after all of that.
                results["_session_alive"] = len((await session.list_tools()).tools)
        return results

    return asyncio.run(main())


CALLS = [
    ("node_status", {}),
    ("node_safety", {}),
    ("node_logs", {"lines": 20}),
    ("node_connect_alpaca", {"host": "127.0.0.1", "port": 0}),   # filled in
    ("node_status", {}),
    ("node_slew", {"ra_hours": 5.5, "dec_deg": 22.0}),
    ("node_park", {}),
    ("node_abort_exposure", {}),
    ("imaging_targets", {"limit": 3}),
    ("imaging_status", {}),
    ("tonight_results", {}),
    ("diagnose", {}),
]


class _FaultCase(unittest.TestCase):
    #: None on the base class, so it is skipped rather than run as a case of
    #: its own; each subclass names the profile it exercises.
    PROFILE = None

    @classmethod
    def setUpClass(cls):
        if cls.PROFILE is None:
            raise unittest.SkipTest("base class")
        cls.rig = _FaultyRig(cls.PROFILE)
        calls = [(n, dict(a)) for n, a in CALLS]
        for name, args in calls:
            if name == "node_connect_alpaca":
                args["port"] = cls.rig.obs.port
        cls.results = drive(cls.rig.base, calls)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "rig", None) is not None:
            cls.rig.stop()

    # ── the invariants ────────────────────────────────────────────────────

    def test_every_call_returned(self):
        """A tool that hangs takes the conversation with it."""
        hung = [n for n, r in self.results.items()
                if isinstance(r, dict) and not r["returned"]]
        self.assertEqual(hung, [], f"these never returned: {hung}")

    def test_nothing_crashed_the_session(self):
        self.assertGreater(self.results["_session_alive"], 40,
                           "the MCP session did not survive the faults")

    def test_failures_are_readable_english(self):
        """Whatever went wrong, a person has to be able to read the reason."""
        for name, r in self.results.items():
            if not isinstance(r, dict) or r["ok"]:
                continue
            self.assertTrue(r["text"].strip(), f"{name} failed with no message")
            self.assertNotIn("Traceback", r["text"],
                             f"{name} leaked a traceback instead of a reason")

    def test_reading_state_keeps_working(self):
        """Diagnosis must not depend on the hardware being healthy -- it is
        what gets reached for precisely when the hardware is not."""
        for name in ("node_status", "node_safety", "diagnose"):
            self.assertTrue(self.results[name]["returned"], f"{name} hung")

    def test_diagnose_says_something_useful(self):
        """It must reach a plain-English verdict, not just dump state."""
        payload = self.results["diagnose"]["payload"]
        self.assertIsInstance(payload, dict)
        self.assertTrue(str(payload.get("summary") or "").strip(),
                        "diagnose returned no summary")
        self.assertIn("logs", payload, "diagnose omitted the logs")

    def test_stopping_activity_always_works(self):
        """The brake must not depend on the thing that broke."""
        self.assertTrue(self.results["node_abort_exposure"]["returned"])

    def test_catalogue_tools_are_unaffected_by_hardware_faults(self):
        """One broken device must not take out tools that never touch it."""
        self.assertTrue(self.results["imaging_targets"]["ok"],
                        self.results["imaging_targets"]["text"])


class CleanRunTest(_FaultCase):
    """Baseline: with no faults injected, the same calls behave."""
    PROFILE = "none"


class BehavioralFaultTest(_FaultCase):
    """Mount stuck slewing, camera never ready, devices rebooting mid-run."""
    PROFILE = "behavioral"


class TransportFaultTest(_FaultCase):
    """The ALPACA server returns 500s, hangs, drops connections, truncates JSON."""
    PROFILE = "transport"


class MixedFaultTest(_FaultCase):
    """Everything at once, which is what a genuinely bad night looks like."""
    PROFILE = "mixed"


if __name__ == "__main__":
    unittest.main()
