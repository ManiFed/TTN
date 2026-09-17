#!/usr/bin/env python3
"""Issue #133 / #127: clear capture-active after completed expose.

After a successful/cancelled expose, next StartExposure within ~5s must succeed
without operator abort — Camera.clear_capture_latch + expose preflight.
"""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import MagicMock, patch

from alpaca.camera import Camera, _STATE_IDLE, _STATE_EXPOSING, _STATE_DOWNLOAD


class ClearCaptureLatchTest(unittest.TestCase):
    def test_clear_capture_latch_noop_when_idle(self):
        cam = Camera.__new__(Camera)
        cam._c = MagicMock()
        cam.camera_state = MagicMock(return_value=_STATE_IDLE)
        self.assertTrue(cam.clear_capture_latch(settle_s=0.1))
        cam._c._put.assert_not_called()

    def test_clear_capture_latch_aborts_when_not_idle(self):
        cam = Camera.__new__(Camera)
        cam._c = MagicMock()
        # First call: DOWNLOAD; after abort settle: IDLE
        cam.camera_state = MagicMock(side_effect=[_STATE_DOWNLOAD, _STATE_IDLE, _STATE_IDLE])
        with patch.object(cam, "_wait_capture_idle", return_value=_STATE_IDLE) as wait:
            ok = cam.clear_capture_latch(settle_s=0.1)
        self.assertTrue(ok)
        cam._c._put.assert_called_with("abortexposure")
        wait.assert_called()

    def test_expose_preflight_clears_stale_latch(self):
        src = inspect.getsource(Camera.expose)
        self.assertIn("clear_capture_latch", src)
        self.assertIn("issue #133", src)
        self.assertIn("startexposure", src)
        # preflight before startexposure
        self.assertLess(src.index("clear_capture_latch"), src.index("startexposure"))

    def test_abort_exposure_uses_wait_capture_idle(self):
        src = inspect.getsource(Camera.abort_exposure)
        self.assertIn("_wait_capture_idle", src)

    def test_dashboard_post_expose_clears_latch(self):
        import src.dashboard as dash
        src = inspect.getsource(dash.api_expose)
        self.assertIn("clear_capture_latch", src)
        self.assertIn("#133", src)
        sched = inspect.getsource(dash._run_schedule_observation)
        self.assertIn("clear_capture_latch", sched)


if __name__ == "__main__":
    unittest.main()
