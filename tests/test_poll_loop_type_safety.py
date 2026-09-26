"""_poll_loop must never store a non-numeric telescope reading.

_state["telescope"]["ra"]/["dec"] get interpolated into f"{ra:.4f}"-style
strings elsewhere (e.g. the manual-exposure target name at
dashboard.py's api_expose worker). Storing whatever _tel.ra()/.dec()
happen to return, unvalidated, means a malformed device response -- or in
tests, a transient mock -- poisons that state until the next successful
poll and crashes formatting far from this loop with a confusing error.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from src import dashboard as dash


class PollLoopTypeSafetyTest(unittest.TestCase):
    def setUp(self):
        with dash._state_lock:
            self._saved_enabled = dict(
                telescope=dash._state["telescope"].get("enabled"),
                camera=dash._state["camera"].get("enabled"),
                focuser=dash._state["focuser"].get("enabled"),
            )
            dash._state["telescope"]["enabled"] = True
            dash._state["camera"]["enabled"] = False
            dash._state["focuser"]["enabled"] = False
        dash._poller_stop.clear()
        self.addCleanup(self._reset)

    def _reset(self):
        dash._poller_stop.set()
        with dash._state_lock:
            dash._state["telescope"]["enabled"] = self._saved_enabled["telescope"]
            dash._state["camera"]["enabled"] = self._saved_enabled["camera"]
            dash._state["focuser"]["enabled"] = self._saved_enabled["focuser"]

    def test_a_mocked_telescope_never_poisons_ra_dec_with_non_numeric_values(self):
        tel = MagicMock()  # .ra()/.dec() default to auto-mocked, non-float values
        with patch.object(dash, "_tel", tel), \
             patch.object(dash, "_cam", None), \
             patch.object(dash, "_foc", None), \
             patch.object(dash, "_cover", None):
            t = threading.Thread(target=dash._poll_loop, daemon=True)
            t.start()
            deadline = time.time() + 5.0
            while time.time() < deadline:
                with dash._state_lock:
                    if dash._state["telescope"].get("connected"):
                        break
                time.sleep(0.02)
            dash._poller_stop.set()
            t.join(timeout=2.0)

        with dash._state_lock:
            ra = dash._state["telescope"]["ra"]
            dec = dash._state["telescope"]["dec"]
        self.assertIsInstance(ra, float)
        self.assertIsInstance(dec, float)
        # Must not raise -- this is exactly what crashed before the fix.
        self.assertTrue(f"Manual RA {ra:.4f}h")


if __name__ == "__main__":
    unittest.main()
