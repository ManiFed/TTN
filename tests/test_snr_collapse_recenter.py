#!/usr/bin/env python3
"""Issue #96: quality=poor / SNR collapse is terminal — recenter and retry,
do not just lengthen expose.

On Starfront, SS Cyg produced a 10s frame (SNR 7.04, quality=poor) and then
a 30s frame (SNR 0.64, quality=poor — *worse* despite tripling the exposure).
Both were correctly skipped for AAVSO, but the node's only response was to
keep lengthening exposure on what was actually a failed lock / dead field.

Expected behavior:
  - classify a poor result as "collapse" (empty-field-like: SNR near zero, or
    SNR getting worse despite a longer exposure) vs "borderline" (a real but
    faint star where more integration is plausible)
  - collapse -> abort the pointing, recenter via the existing centering
    primitive, retake one NAMED frame at the ORIGINAL exposure (not longer)
  - borderline -> leave it alone; more frames/integration is fine
  - quality=poor is never submitted to AAVSO and last_submission stays
    "skipped" until a frame actually passes the (unmodified) quality gate
  - issues #90 (target/AUID override) and #91 (ASTAP->VSP/AUID fallback)
    behavior is untouched

Run with:  python3 -m pytest tests/test_snr_collapse_recenter.py
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import src.dashboard as dash
import src.aavso_submission as aavso_submission
from src.photometry import classify_snr_failure


def _poor_result(target="SS Cyg", snr=7.04, **over):
    base = {
        "target_name": target,
        "bjd": 2461000.5,
        "magnitude": 11.9,
        "uncertainty": 0.4,
        "snr": snr,
        "quality_flag": "poor",
        "quality_reasons": [{"check": "snr", "value": snr,
                             "threshold": 10.0, "outcome": "fail"}],
        "sky_mag": None,
        "ra_deg": 325.68,
        "dec_deg": 43.59,
    }
    base.update(over)
    return base


class ClassifySnrFailureTest(unittest.TestCase):
    """Pure classification logic — no dashboard/hardware involved."""

    def test_near_zero_snr_is_collapse(self):
        self.assertEqual(classify_snr_failure(0.64, 30.0, 7.04, 10.0), "collapse")

    def test_snr_worse_after_longer_exposure_is_collapse(self):
        # Same numbers as the Starfront night report: 10s->30s, SNR 7.04->0.64.
        self.assertEqual(
            classify_snr_failure(snr=0.64, exp_dur=30.0,
                                 prior_snr=7.04, prior_exp_dur=10.0),
            "collapse",
        )

    def test_snr_improving_with_longer_exposure_is_borderline(self):
        self.assertEqual(
            classify_snr_failure(snr=12.0, exp_dur=30.0,
                                 prior_snr=7.0, prior_exp_dur=10.0),
            "borderline",
        )

    def test_first_attempt_low_but_plausible_snr_is_borderline(self):
        self.assertEqual(classify_snr_failure(snr=7.04, exp_dur=10.0), "borderline")

    def test_no_prior_and_no_exp_dur_defaults_to_borderline_unless_near_zero(self):
        self.assertEqual(classify_snr_failure(snr=6.0), "borderline")
        self.assertEqual(classify_snr_failure(snr=1.5), "collapse")

    def test_shorter_retry_with_lower_snr_is_not_penalized_as_collapse(self):
        # exp_dur did not increase, so a lower SNR isn't the "longer exposure
        # made it worse" signature -- e.g. clouds thinned then thickened again.
        self.assertEqual(
            classify_snr_failure(snr=5.0, exp_dur=10.0,
                                 prior_snr=7.0, prior_exp_dur=10.0),
            "borderline",
        )


class HandlePoorQualityResultTest(unittest.TestCase):
    """dash._handle_poor_quality_result: classification + recenter wiring."""

    def setUp(self):
        with dash._poor_quality_lock:
            dash._poor_quality_last.clear()
            dash._poor_quality_retries.clear()
        self.addCleanup(self._reset)

    def _reset(self):
        with dash._poor_quality_lock:
            dash._poor_quality_last.clear()
            dash._poor_quality_retries.clear()

    def test_first_poor_result_is_borderline_and_does_not_recenter(self):
        retry = MagicMock()
        with patch.object(dash, "_recenter_and_retry_target", retry), \
             patch.object(dash, "_read_exptime_s", return_value=10.0):
            dash._handle_poor_quality_result(
                _poor_result(snr=7.04), "/tmp/ss_cyg_10s.fits", {"photometry": {}})
        retry.assert_not_called()

    def test_snr_collapse_on_second_longer_frame_triggers_recenter(self):
        retry = MagicMock()
        cfg = {"photometry": {}}
        with patch.object(dash, "_recenter_and_retry_target", retry), \
             patch.object(dash, "_read_exptime_s", return_value=10.0):
            dash._handle_poor_quality_result(
                _poor_result(snr=7.04), "/tmp/ss_cyg_10s.fits", cfg)
        retry.assert_not_called()

        with patch.object(dash, "_recenter_and_retry_target", retry), \
             patch.object(dash, "_read_exptime_s", return_value=30.0):
            dash._handle_poor_quality_result(
                _poor_result(snr=0.64), "/tmp/ss_cyg_30s.fits", cfg)
        retry.assert_called_once()
        target_arg = retry.call_args[0][0]
        self.assertEqual(target_arg, "SS Cyg")
        # Must retry at the ORIGINAL exposure, not the longer one that failed.
        retry_exp_dur = retry.call_args.kwargs.get("retry_exp_dur", retry.call_args[0][-1])
        self.assertEqual(retry_exp_dur, 10.0)

    def test_recenter_retry_is_capped_so_it_cannot_loop_forever(self):
        cfg = {"photometry": {}}
        retry = MagicMock()
        with patch.object(dash, "_recenter_and_retry_target", retry), \
             patch.object(dash, "_read_exptime_s", return_value=10.0):
            dash._handle_poor_quality_result(
                _poor_result(snr=0.5), "/tmp/f1.fits", cfg)
        self.assertEqual(retry.call_count, 1)

        with patch.object(dash, "_recenter_and_retry_target", retry), \
             patch.object(dash, "_read_exptime_s", return_value=10.0):
            dash._handle_poor_quality_result(
                _poor_result(snr=0.5), "/tmp/f2.fits", cfg)
        # Budget for this streak is already spent -- must not loop forever.
        self.assertEqual(retry.call_count, 1)

    def test_a_passing_result_resets_the_streak_for_next_time(self):
        cfg = {"photometry": {}}
        retry = MagicMock()
        with patch.object(dash, "_recenter_and_retry_target", retry), \
             patch.object(dash, "_read_exptime_s", return_value=10.0):
            dash._handle_poor_quality_result(
                _poor_result(snr=0.5), "/tmp/f1.fits", cfg)
        self.assertEqual(retry.call_count, 1)

        with dash._poor_quality_lock:
            dash._poor_quality_last.pop("SS Cyg", None)
            dash._poor_quality_retries.pop("SS Cyg", None)

        with patch.object(dash, "_recenter_and_retry_target", retry), \
             patch.object(dash, "_read_exptime_s", return_value=10.0):
            dash._handle_poor_quality_result(
                _poor_result(snr=0.5), "/tmp/f2.fits", cfg)
        self.assertEqual(retry.call_count, 2)


class RecenterAndRetryTargetTest(unittest.TestCase):
    """dash._recenter_and_retry_target: it must reuse the existing centering
    primitive (not invent new centering code) and retake a NAMED frame at the
    original exposure."""

    def test_uses_existing_centering_primitive_with_degrees(self):
        centering = MagicMock()
        cam = MagicMock()
        enqueued = []
        with tempfile.TemporaryDirectory() as td, \
             patch.object(dash, "_tel", MagicMock()), \
             patch.object(dash, "_cam", cam), \
             patch.object(dash, "_run_centering_bg", centering), \
             patch.object(dash, "_capture_image", return_value=""), \
             patch("pathlib.Path.exists", return_value=True), \
             patch.object(dash, "_enqueue_photometry",
                          side_effect=lambda p, target_name=None, auid=None:
                              enqueued.append((p, target_name))), \
             patch("os.getcwd", return_value=td):
            dash._recenter_and_retry_target(
                "SS Cyg",
                _poor_result(snr=0.64, ra_deg=325.68, dec_deg=43.59),
                {"photometry": {"target": {"ra_deg": 325.68, "dec_deg": 43.59}}},
                retry_exp_dur=10.0,
            )
        centering.assert_called_once()
        args = centering.call_args[0]
        self.assertAlmostEqual(args[0], 325.68, places=2)
        self.assertAlmostEqual(args[1], 43.59, places=2)
        cam.expose.assert_called_once()
        self.assertEqual(cam.expose.call_args.kwargs.get("duration"), 10.0)
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0][1], "SS Cyg")

    def test_no_telescope_connected_does_not_raise(self):
        with patch.object(dash, "_tel", None), patch.object(dash, "_cam", None):
            dash._recenter_and_retry_target(
                "SS Cyg", _poor_result(), {"photometry": {}}, retry_exp_dur=10.0,
            )  # must not raise

    def test_no_coordinates_available_does_not_raise_or_recenter(self):
        centering = MagicMock()
        with patch.object(dash, "_tel", MagicMock()), \
             patch.object(dash, "_cam", MagicMock()), \
             patch.object(dash, "_run_centering_bg", centering):
            result = _poor_result()
            result.pop("ra_deg")
            result.pop("dec_deg")
            dash._recenter_and_retry_target(
                "SS Cyg", result, {"photometry": {}}, retry_exp_dur=10.0,
            )
        centering.assert_not_called()


class QualityGateUnmodifiedTest(unittest.TestCase):
    """Issue #96 explicitly forbids weakening the AAVSO quality gate or ever
    submitting quality=poor — this only changes retry behavior."""

    def test_poor_quality_is_never_submitted(self):
        cfg = {"aavso": {"observer_code": "ABC"}}
        result = _poor_result()
        sub = aavso_submission.submit(result, cfg)
        self.assertEqual(sub["status"], "skipped")
        self.assertEqual(sub["accepted"], 0)

    def test_last_submission_stays_skipped_through_a_full_poor_frame_cycle(self):
        cfg = {
            "aavso": {"observer_code": "ABC"},
            "photometry": {},
        }
        with dash._state_lock:
            dash._state["aavso"]["last_submission"] = None
        with patch("src.photometry.run_pipeline_ex",
                   return_value=(_poor_result(), None)), \
             patch.object(dash, "_load_config", return_value=cfg), \
             patch.object(dash, "_export_fits", return_value=None), \
             patch.object(dash, "_handle_poor_quality_result", MagicMock()), \
             patch.object(dash, "_cloud", None):
            dash._run_photometry_bg("/tmp/ss_cyg.fits")
        with dash._state_lock:
            sub = dash._state["aavso"]["last_submission"]
        self.assertIsNotNone(sub)
        self.assertEqual(sub["status"], "skipped")

    def test_run_photometry_bg_routes_poor_results_to_the_handler(self):
        handler = MagicMock()
        cfg = {"photometry": {}}
        with patch("src.photometry.run_pipeline_ex",
                   return_value=(_poor_result(), None)), \
             patch.object(dash, "_load_config", return_value=cfg), \
             patch.object(dash, "_export_fits", return_value=None), \
             patch.object(dash, "_handle_poor_quality_result", handler), \
             patch.object(dash, "_cloud", None):
            dash._run_photometry_bg("/tmp/ss_cyg.fits")
        handler.assert_called_once()
        self.assertEqual(handler.call_args[0][0]["quality_flag"], "poor")
        self.assertEqual(handler.call_args[0][1], "/tmp/ss_cyg.fits")

    def test_run_photometry_bg_does_not_call_handler_for_a_good_result(self):
        handler = MagicMock()
        good = _poor_result()
        good["quality_flag"] = "good"
        cfg = {"photometry": {}}
        with patch("src.photometry.run_pipeline_ex", return_value=(good, None)), \
             patch.object(dash, "_load_config", return_value=cfg), \
             patch.object(dash, "_export_fits", return_value=None), \
             patch.object(dash, "_handle_poor_quality_result", handler), \
             patch.object(dash, "_cloud", None):
            dash._run_photometry_bg("/tmp/ss_cyg.fits")
        handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
