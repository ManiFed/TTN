"""Gauntlet: live fleet realtime infrastructure (Phase 0).

Asserts the node-side pieces of the live state pipeline against a programmable
fake cloud — no telescope, no Postgres:

  * an SSE push of "interrupt" makes the node fetch interrupts within ~1 s
    (vs the 300 s poll fallback),
  * the heartbeat carries the live `state` block and adapts its cadence to
    whether the node is observing.
"""

import threading
import time
import unittest

from src.cloud_communicator import CloudCommunicator
from tests.gauntlet.fakecloud import FakeCloud
from tests.gauntlet.util import TempCwdTestCase


def _config(url: str, **overrides) -> dict:
    cloud = {
        "enabled": True,
        "url": url,
        "realtime_url": url,
        "node_id": "node_test01",
        "api_key": "key_test01",
        "heartbeat_interval": 3600,      # slow — isolate SSE-driven polling
        "heartbeat_fast_interval": 1,
        "plan_poll_interval": 3600,      # slow — any fast poll came from SSE
    }
    cloud.update(overrides)
    return {
        "cloud": cloud,
        "observatory": {"latitude": 31.36, "longitude": -99.44,
                        "elevation": 500, "observer": "Gauntlet"},
        "photometry": {"node_id": "node_test01", "filter_name": "CV"},
    }


class LiveFleetRealtimeTest(TempCwdTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeCloud().start()

    def tearDown(self):
        self.fake.stop()
        super().tearDown()

    def test_sse_interrupt_triggers_fast_poll(self):
        """A pushed 'interrupt' event fetches interrupts far under the poll TTL."""
        received: list[dict] = []
        got = threading.Event()

        def on_interrupt(item):
            received.append(item)
            got.set()

        self.fake.interrupts = [{
            "id": 1, "name": "AT2026zzz", "ra_deg": 210.0, "dec_deg": 12.0,
            "reason": "reflex_confirm", "acked": False,
        }]
        comm = CloudCommunicator(_config(self.fake.url), on_interrupt=on_interrupt)
        # Drive just the SSE loop so the result can't come from the plan poll.
        t = threading.Thread(target=comm._sse_loop, daemon=True)
        t.start()
        try:
            time.sleep(0.4)          # let the stream connect
            self.fake.push("interrupt")
            self.assertTrue(got.wait(timeout=2.0),
                            "interrupt not delivered within 2 s of SSE push")
            self.assertEqual(received[0]["name"], "AT2026zzz")
            self.assertGreaterEqual(self.fake.count_paths("/interrupts"), 1)
        finally:
            comm.stop()

    def test_heartbeat_carries_state_and_adapts_cadence(self):
        """The heartbeat reports live phase; cadence is fast while observing."""
        comm = CloudCommunicator(
            _config(self.fake.url),
            get_state=lambda: {"phase": "exposing", "is_dark": True,
                               "target_name": "V1234 Cyg"},
        )
        t = threading.Thread(target=comm._heartbeat_loop, daemon=True)
        t.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and not self.fake.last_state():
                time.sleep(0.1)
            state = self.fake.last_state()
            self.assertEqual(state.get("phase"), "exposing")
            self.assertTrue(state.get("is_dark"))
            # Dark + exposing selects the fast cadence.
            self.assertEqual(comm._heartbeat_interval(state), 1)
            # Idle daylight relaxes to the slow cadence.
            self.assertEqual(
                comm._heartbeat_interval({"phase": "idle", "is_dark": False}),
                3600)
        finally:
            comm.stop()


if __name__ == "__main__":
    unittest.main()
