#!/usr/bin/env python3
"""Starfront 2026-09-20/21 soft bugs after NodeAgent 1.0.91.

1. Mid-expose re-slew / schedule+recenter hijack while node_expose owns camera
   (observed Dec +61° path, Error 1279 aborts, no FITS).
2. Science stack export disabled by default (export_science:false, export_path:null)
   blocking AAVSO coadd SNR path for faint SS Cyg (related closed #132).

Related closed: #131 mid-expose drift, #133 Error 1279, #135 uncommanded slew,
#132 science coadd, #116/#120 pointing jumps.
"""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import MagicMock, patch

import src.dashboard as dash


class ManualExposeOwnsCameraTest(unittest.TestCase):
    def setUp(self):
        self._prev = dict(dash._state["camera"])
        self.addCleanup(self._restore)

    def _restore(self):
        with dash._state_lock:
            dash._state["camera"].update(self._prev)

    def test_true_when_camera_exposing(self):
        with dash._state_lock:
            dash._state["camera"]["exposing"] = True
        self.assertTrue(dash._manual_expose_owns_camera())

    def test_false_when_idle(self):
        with dash._state_lock:
            dash._state["camera"]["exposing"] = False
        self.assertFalse(dash._manual_expose_owns_camera())


class RecenterHijackRefuseTest(unittest.TestCase):
    def test_recenter_skips_while_science_capture_active(self):
        src = inspect.getsource(dash._recenter_and_retry_target)
        self.assertIn("_science_capture_active", src)
        self.assertIn("mid-expose hijack", src)

    def test_recenter_returns_without_centering_when_exposing(self):
        calls = []
        with patch.object(dash, "_science_capture_active", return_value=True), \
             patch.object(dash, "_run_centering_bg",
                          side_effect=lambda *a, **k: calls.append(1)), \
             patch.object(dash, "_tel", MagicMock()), \
             patch.object(dash, "_cam", MagicMock()):
            dash._recenter_and_retry_target(
                "T CrB",
                {"ra_deg": 239.8, "dec_deg": 25.9},
                {"photometry": {"target": {"ra_deg": 239.8, "dec_deg": 25.9}}},
                retry_exp_dur=30.0,
            )
        self.assertEqual(calls, [])


class ScheduleSlewHijackRefuseTest(unittest.TestCase):
    def test_schedule_observation_checks_manual_expose(self):
        src = inspect.getsource(dash._run_schedule_observation)
        self.assertIn("_manual_expose_owns_camera", src)
        self.assertIn("mid-expose hijack", src)

    def test_reslew_helper_clears_latch_and_refuses_manual(self):
        src = inspect.getsource(dash._run_schedule_observation)
        self.assertIn("_clear_latch_before_recovery_slew", src)
        self.assertIn("_manual_expose_owns_camera", src)


class NudgeMoveAxisRefuseTest(unittest.TestCase):
    def test_nudge_refuses_during_science_capture(self):
        src = inspect.getsource(dash.api_nudge)
        self.assertIn("_science_capture_active", src)
        self.assertIn("mid-expose hijack", src)

    def test_moveaxis_refuses_during_science_capture(self):
        src = inspect.getsource(dash.api_move_axis)
        self.assertIn("_science_capture_active", src)


class ClearLatchBeforeRecoveryTest(unittest.TestCase):
    def test_helper_exists_and_calls_clear(self):
        self.assertTrue(hasattr(dash, "_clear_latch_before_recovery_slew"))
        cam = MagicMock()
        with patch.object(dash, "_cam", cam):
            dash._clear_latch_before_recovery_slew(label="test")
        cam.clear_capture_latch.assert_called_once()

    def test_manual_reslew_source_clears_latch(self):
        src = inspect.getsource(dash.api_expose)
        self.assertIn("_clear_latch_before_recovery_slew", src)


class ScienceExportDefaultTest(unittest.TestCase):
    def test_api_stack_start_defaults_export_science_true(self):
        src = inspect.getsource(dash.api_stack_start)
        self.assertIn('cfg.get("export_science", True)', src)
        # Named target forces export when flag omitted.
        self.assertIn("export_science", src)
        self.assertIn("target_name", src)

    def test_run_stacking_bg_default_true(self):
        sig = inspect.signature(dash._run_stacking_bg)
        self.assertTrue(sig.parameters["export_science"].default is True)

    def test_mcp_start_stacking_passes_export_science(self):
        from telescope_mcp.tools import images as images_mod
        src = inspect.getsource(images_mod.register)
        self.assertIn("export_science", src)
        self.assertIn("/api/stack/export", src)
        self.assertIn("export_science: bool = True", src)
        self.assertIn('"export_science": True', src)
        self.assertIn("def export_stack", src)


if __name__ == "__main__":
    unittest.main()
