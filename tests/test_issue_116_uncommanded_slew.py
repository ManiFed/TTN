#!/usr/bin/env python3
"""Issue #116: Seestar uncommanded slew / empty-field / preview-during-capture.

2026-09-13 Starfront night (NodeAgent 1.0.81, Seestar S50):
  - Named T CrB expose wrote a FITS but photometry rejected BEFORE SNR:
    no target/comparison stars in the field.
  - Second frame: SET_PREVIEW_PAGE fail: capture is active (Seestar app was
    concurrently commanding the mount — NodeAgent does not issue that
    firmware command itself).
  - Mount drifted off T CrB (later seen at a Cygnus-ish RA 20.22h /
    Dec +38.4, Vega-like Dec) mid-expose; #96 recenter never ran because
    there was no SNR score.

Expected:
  - Preview / live-stack / centering / autofocus refuse to start while a
    science capture is active (`_preview_commands_allowed`).
  - Empty-field rejection uses the same recenter+retry path as SNR collapse.
  - Off-target ALPACA RA/Dec (Vega or Cygnus-ish vs commanded T CrB) is
    detected at ~1° tolerance so the node aborts/reslews rather than
    photometrying a wrong field.

Run with:  python3 -m pytest tests/test_issue_116_uncommanded_slew.py
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import src.dashboard as dash
from src.photometry import (
    EMPTY_FIELD_REASON_CODES,
    is_empty_field_rejection,
)


# Starfront numbers (2026-09-13 diagnostic + issue body).
T_CRB_RA_H = 239.87567 / 15.0          # ≈ 15.9917 h
T_CRB_DEC = 25.92017
VEGA_RA_H = 279.23 / 15.0              # ≈ 18.615 h
VEGA_DEC = 38.78
# Fresh diagnostic after quitting the Seestar app: Cygnus-ish, not Vega.
CYGNUS_RA_H = 20.218889
CYGNUS_DEC = 38.416667


class EmptyFieldRejectionHelperTest(unittest.TestCase):
    def test_known_empty_field_codes(self):
        for code in (
            "too_few_comparison_stars",
            "no_comparison_stars",
            "target_off_frame",
        ):
            self.assertIn(code, EMPTY_FIELD_REASON_CODES)
            self.assertTrue(
                is_empty_field_rejection({"reason_code": code}),
                msg=code,
            )

    def test_snr_or_unrelated_reject_is_not_empty_field(self):
        self.assertFalse(is_empty_field_rejection({"reason_code": "nonpositive_target_flux"}))
        self.assertFalse(is_empty_field_rejection({"reason_code": "plate_solve_failed"}))
        self.assertFalse(is_empty_field_rejection(None))
        self.assertFalse(is_empty_field_rejection({}))


class PointingOffTargetTest(unittest.TestCase):
    """Pure pointing math — T CrB vs Vega and vs the Cygnus-ish diagnostic."""

    def test_on_target_is_not_off(self):
        self.assertFalse(
            dash._pointing_off_target(
                T_CRB_RA_H, T_CRB_DEC, T_CRB_RA_H, T_CRB_DEC, tol_deg=1.0,
            )
        )

    def test_small_settle_error_within_one_degree_is_ok(self):
        # ~0.2° settle error should pass a 1° gate.
        self.assertFalse(
            dash._pointing_off_target(
                T_CRB_RA_H + 0.01, T_CRB_DEC + 0.2,
                T_CRB_RA_H, T_CRB_DEC, tol_deg=1.0,
            )
        )

    def test_vega_vs_t_crb_is_off_target(self):
        self.assertTrue(
            dash._pointing_off_target(
                VEGA_RA_H, VEGA_DEC, T_CRB_RA_H, T_CRB_DEC, tol_deg=1.0,
            )
        )

    def test_cygnus_ish_starfront_pointing_vs_t_crb_is_off_target(self):
        # RA 20.22h Dec +38.4 vs T CrB 15.99h / +25.9 — the post-app-quit
        # diagnostic. Must be caught, not only a Vega match.
        self.assertTrue(
            dash._pointing_off_target(
                CYGNUS_RA_H, CYGNUS_DEC, T_CRB_RA_H, T_CRB_DEC, tol_deg=1.0,
            )
        )


class PreviewGateDuringCaptureTest(unittest.TestCase):
    """SET_PREVIEW_PAGE-like / preview work must not start during capture."""

    def setUp(self):
        self._prev_exposing = dash._state["camera"].get("exposing")
        self._prev_phase = dash._sched_state.get("current_phase")
        self._prev_running = dash._sched_state.get("running")
        with dash._state_lock:
            dash._state["camera"]["exposing"] = False
        with dash._sched_lock:
            dash._sched_state["running"] = False
            dash._sched_state["current_phase"] = ""

    def tearDown(self):
        with dash._state_lock:
            dash._state["camera"]["exposing"] = self._prev_exposing
        with dash._sched_lock:
            dash._sched_state["running"] = self._prev_running
            dash._sched_state["current_phase"] = self._prev_phase

    def test_preview_allowed_when_idle(self):
        self.assertTrue(dash._preview_commands_allowed())
        self.assertFalse(dash._science_capture_active())

    def test_manual_expose_blocks_preview(self):
        with dash._state_lock:
            dash._state["camera"]["exposing"] = True
        self.assertTrue(dash._science_capture_active())
        self.assertFalse(dash._preview_commands_allowed())

    def test_schedule_exposing_phase_blocks_preview(self):
        with dash._sched_lock:
            dash._sched_state["running"] = True
            dash._sched_state["current_phase"] = "exposing"
        self.assertTrue(dash._science_capture_active())
        self.assertFalse(dash._preview_commands_allowed())

    def test_stack_start_returns_409_while_exposing(self):
        # Exercise the HTTP gate without a real camera: the exposing check
        # must fire before the "camera not connected" branch would... actually
        # camera-not-connected is checked first. Patch _cam and the other
        # running-state locks so we reach the preview gate.
        with dash._state_lock:
            dash._state["camera"]["exposing"] = True
        fake_cam = MagicMock()
        with patch.object(dash, "_cam", fake_cam), \
             patch.object(dash, "_stack_state", {"running": False}), \
             patch.object(dash, "_focus_state", {"running": False}), \
             patch.object(dash, "_center_state", {"running": False}):
            client = dash.app.test_client()
            resp = client.post("/api/stack/start", json={"frames": 2, "exposure_s": 1.0})
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertIn("Science capture active", body.get("error", ""))


class EmptyFieldTriggersRecenterTest(unittest.TestCase):
    def setUp(self):
        with dash._poor_quality_lock:
            dash._poor_quality_last.clear()
            dash._poor_quality_retries.clear()
        self.addCleanup(self._reset)

    def _reset(self):
        with dash._poor_quality_lock:
            dash._poor_quality_last.clear()
            dash._poor_quality_retries.clear()

    def test_empty_field_reject_calls_recenter_once(self):
        rejection = {
            "rejected": True,
            "stage": "comp_stars",
            "reason_code": "too_few_comparison_stars",
            "message": "fewer than 2 comparison stars land in the frame",
            "target_name": "T CrB",
            "detail": {"ra_deg": 239.87567, "dec_deg": 25.92017, "n_in_field": 0},
        }
        cfg = {"photometry": {"target": {"name": "T CrB",
                                         "ra_deg": 239.87567,
                                         "dec_deg": 25.92017},
                              "centering_exposure_s": 3.0}}
        retry = MagicMock()
        with patch.object(dash, "_recenter_and_retry_target", retry), \
             patch.object(dash, "_read_exptime_s", return_value=20.0), \
             patch.object(dash, "_telemetry", MagicMock()):
            dash._handle_empty_field_rejection(rejection, "T_CrB_01.fits", cfg)
        retry.assert_called_once()
        args, kwargs = retry.call_args
        self.assertEqual(args[0], "T CrB")
        self.assertEqual(kwargs.get("retry_exp_dur"), 20.0)
        with dash._poor_quality_lock:
            self.assertEqual(dash._poor_quality_retries.get("T CrB"), 1)

    def test_empty_field_shares_retry_budget_with_snr_collapse(self):
        rejection = {
            "reason_code": "no_comparison_stars",
            "target_name": "T CrB",
            "detail": {"ra_deg": 239.87567, "dec_deg": 25.92017},
        }
        cfg = {"photometry": {"target": {"name": "T CrB",
                                         "ra_deg": 239.87567,
                                         "dec_deg": 25.92017}}}
        with dash._poor_quality_lock:
            dash._poor_quality_retries["T CrB"] = 1  # already used by #96 path
        retry = MagicMock()
        with patch.object(dash, "_recenter_and_retry_target", retry), \
             patch.object(dash, "_read_exptime_s", return_value=20.0), \
             patch.object(dash, "_telemetry", MagicMock()):
            dash._handle_empty_field_rejection(rejection, "T_CrB_01.fits", cfg)
        retry.assert_not_called()

    def test_run_photometry_bg_routes_empty_field_to_handler(self):
        rejection = {
            "rejected": True,
            "stage": "comp_stars",
            "reason_code": "too_few_comparison_stars",
            "message": "no target/comparison stars in the field",
            "target_name": "T CrB",
            "detail": {"ra_deg": 239.87567, "dec_deg": 25.92017},
        }
        handler = MagicMock()
        cfg = {"photometry": {}}
        with patch("src.photometry.run_pipeline_ex", return_value=(None, rejection)), \
             patch.object(dash, "_load_config", return_value=cfg), \
             patch.object(dash, "_handle_empty_field_rejection", handler), \
             patch.object(dash, "_cloud", None), \
             patch.object(dash, "_telemetry", MagicMock()):
            dash._run_photometry_bg(
                "T_CrB_01.fits",
                target_name="T CrB",
                ra_deg=239.87567,
                dec_deg=25.92017,
            )
        handler.assert_called_once()
        with dash._state_lock:
            rej = dash._state["photometry"]["last_rejection"]
        self.assertIsNotNone(rej)
        self.assertEqual(rej["reason_code"], "too_few_comparison_stars")



class VerifyPointingIntegrationTest(unittest.TestCase):
    def test_verify_pointing_false_when_mount_at_cygnus_commanded_t_crb(self):
        fake_tel = MagicMock()
        fake_tel.ra.return_value = CYGNUS_RA_H
        fake_tel.dec.return_value = CYGNUS_DEC
        fake_tel.is_slewing.return_value = False
        with patch.object(dash, "_tel", fake_tel):
            ok = dash._verify_pointing(T_CRB_RA_H, T_CRB_DEC, label="unit")
        self.assertFalse(ok)

    def test_verify_pointing_false_when_slewing(self):
        fake_tel = MagicMock()
        fake_tel.ra.return_value = T_CRB_RA_H
        fake_tel.dec.return_value = T_CRB_DEC
        fake_tel.is_slewing.return_value = True
        with patch.object(dash, "_tel", fake_tel):
            ok = dash._verify_pointing(T_CRB_RA_H, T_CRB_DEC, label="unit")
        self.assertFalse(ok)

    def test_mount_left_target_true_for_vega(self):
        fake_tel = MagicMock()
        fake_tel.ra.return_value = VEGA_RA_H
        fake_tel.dec.return_value = VEGA_DEC
        fake_tel.is_slewing.return_value = False
        with patch.object(dash, "_tel", fake_tel):
            self.assertTrue(dash._mount_left_target(T_CRB_RA_H, T_CRB_DEC))


if __name__ == "__main__":
    unittest.main()
