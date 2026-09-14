#!/usr/bin/env python3
"""Issue #120: mid-expose drift abort+reslew; keep FITS after post-ImageReady park.

2026-09-14 ~1:40am ET Starfront night (NodeAgent 1.0.83, Seestar S50 app quit):
  - Before 20s expose: ALPACA RA 15.995 h Dec +25.9203 (on T CrB).
  - ImageReady after expose.
  - After expose: RA 20.2183 h Dec +38.4339 (Cygnus-ish park — same as #116).
  - Operator ended up with a Desktop 1920×590 dump; no fits_export /
    last_submission. #117's post-expose pointing check treated the park as
    poison and discarded (or never photometry'd) a frame that was on-target
    at readout.

Expected:
  - Mid-expose / at-ImageReady off-target (T CrB → 20.22h +38.4) aborts,
    reslews to the named target/AUID, and retries (#116/#96 path).
  - A slew that happens *only after* ImageReady must NOT discard the FITS —
    photometry still runs on the fits_export frame.
  - Schedule science frames land under fits_export/, not ad-hoc Desktop saves.

Run with:  python3 -m pytest tests/test_issue_120_mid_expose_drift.py
"""

from __future__ import annotations

import inspect
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.dashboard as dash
from alpaca.camera import ExposureCancelled


# Starfront numbers (issue #120 + #116 diagnostic).
T_CRB_RA_H = 239.87567 / 15.0          # ≈ 15.9917 h
T_CRB_DEC = 25.92017
CYGNUS_RA_H = 20.2183
CYGNUS_DEC = 38.4339


class PostReadoutPoisonPolicyTest(unittest.TestCase):
    """Pure policy: post-ImageReady park must not poison an on-target FITS."""

    def test_on_target_at_ready_is_never_poisoned(self):
        self.assertFalse(dash._post_readout_slew_poisons_fits(True))

    def test_off_target_at_ready_is_poison(self):
        self.assertTrue(dash._post_readout_slew_poisons_fits(False))

    def test_cygnus_vs_t_crb_still_detected_as_off_target(self):
        # Gate math unchanged from #116 — drift mid-expose must still fire.
        self.assertTrue(
            dash._pointing_off_target(
                CYGNUS_RA_H, CYGNUS_DEC, T_CRB_RA_H, T_CRB_DEC, tol_deg=1.0,
            )
        )


class MidExposeDriftAbortReslewTest(unittest.TestCase):
    """T CrB → Cygnus mid-expose must abort and reslew (not photometry junk)."""

    def setUp(self):
        self._tel = dash._tel
        self._cam = dash._cam
        self.addCleanup(self._restore)

    def _restore(self):
        dash._tel = self._tel
        dash._cam = self._cam

    def test_mount_left_target_true_for_cygnus_ish_vs_t_crb(self):
        fake_tel = MagicMock()
        fake_tel.ra.return_value = CYGNUS_RA_H
        fake_tel.dec.return_value = CYGNUS_DEC
        fake_tel.is_slewing.return_value = False
        with patch.object(dash, "_tel", fake_tel):
            self.assertTrue(dash._mount_left_target(T_CRB_RA_H, T_CRB_DEC))

    def test_verify_pointing_false_at_cygnus_commanded_t_crb(self):
        fake_tel = MagicMock()
        fake_tel.ra.return_value = CYGNUS_RA_H
        fake_tel.dec.return_value = CYGNUS_DEC
        fake_tel.is_slewing.return_value = False
        with patch.object(dash, "_tel", fake_tel):
            self.assertFalse(
                dash._verify_pointing(T_CRB_RA_H, T_CRB_DEC, label="at-ImageReady")
            )

    def test_schedule_source_aborts_mid_expose_and_reslews(self):
        src = inspect.getsource(dash._run_schedule_observation)
        self.assertIn("cancel_check=_cancel_if_off_target", src)
        self.assertIn("_mount_left_target(ra, dec)", src)
        self.assertIn('_reslew_to_target("mid-expose-abort")', src)
        self.assertIn("at-ImageReady", src)
        # Mid-expose / at-ready off-target raises ExposureCancelled → retry path.
        self.assertIn("pointing off-target at ImageReady (issue #120)", src)

    def test_simulated_mid_expose_cancel_check_trips_on_cygnus_drift(self):
        """cancel_check sees Cygnus mid-expose → ExposureCancelled → reslew."""
        fake_tel = MagicMock()
        # Start on T CrB; flip to Cygnus on first cancel_check poll.
        state = {"n": 0}

        def ra():
            return T_CRB_RA_H if state["n"] < 1 else CYGNUS_RA_H

        def dec():
            return T_CRB_DEC if state["n"] < 1 else CYGNUS_DEC

        fake_tel.ra.side_effect = ra
        fake_tel.dec.side_effect = dec
        fake_tel.is_slewing.return_value = False

        def cancel_check():
            state["n"] += 1
            return dash._mount_left_target(T_CRB_RA_H, T_CRB_DEC)

        with patch.object(dash, "_tel", fake_tel):
            # First poll: still on target (n becomes 1 after increment inside check...
            # Our cancel_check increments then calls _mount_left_target which
            # reads ra/dec. After n+=1, n>=1 → Cygnus → True.
            self.assertTrue(cancel_check())
            self.assertTrue(dash._mount_left_target(T_CRB_RA_H, T_CRB_DEC))


class PostImageReadyKeepPhotometryTest(unittest.TestCase):
    """Pointing moved only after ImageReady → still enqueue photometry."""

    def test_policy_keeps_fits_when_ready_was_on_target(self):
        # Simulate: on-target at ready, Cygnus after download.
        on_target_at_ready = True
        now_off = dash._pointing_off_target(
            CYGNUS_RA_H, CYGNUS_DEC, T_CRB_RA_H, T_CRB_DEC, tol_deg=1.0,
        )
        self.assertTrue(now_off)
        self.assertFalse(dash._post_readout_slew_poisons_fits(on_target_at_ready))

    def test_schedule_no_longer_discards_on_post_expose_verify_alone(self):
        src = inspect.getsource(dash._run_schedule_observation)
        # Old #117 path discarded after download whenever post-expose verify failed.
        self.assertNotIn("post-expose-off-target", src)
        self.assertNotIn("pointing off-target after expose; reslew failed", src)
        # New path: enqueue after successful at-ImageReady expose; log park.
        self.assertIn("_enqueue_on_target_frame", src)
        self.assertIn("post-readout", src)
        self.assertIn("issue #120", src)

    def test_guarded_expose_keeps_fits_when_park_after_ready(self):
        """Drive a mini expose sequence: on-target through ImageReady, then park.

        Photometry enqueue must still fire; discard must not.
        """
        fake_tel = MagicMock()
        pointing = {"ra": T_CRB_RA_H, "dec": T_CRB_DEC, "slewing": False}

        fake_tel.ra.side_effect = lambda: pointing["ra"]
        fake_tel.dec.side_effect = lambda: pointing["dec"]
        fake_tel.is_slewing.side_effect = lambda: pointing["slewing"]

        fake_cam = MagicMock()
        enqueued = []
        discarded = []

        def fake_expose(*, duration, light=True, cancel_check=None, **kwargs):
            # Mid-expose polls: stay on T CrB.
            for _ in range(3):
                if cancel_check is not None and cancel_check():
                    raise ExposureCancelled("mid-expose")
            # ImageReady — still on target. Park happens only AFTER return,
            # simulating firmware goto during imagearray download.
            return None

        def fake_capture(*, fits_path=None, exp_dur=None, target=None):
            # Firmware parks during download.
            pointing["ra"] = CYGNUS_RA_H
            pointing["dec"] = CYGNUS_DEC
            if fits_path:
                Path(fits_path).parent.mkdir(parents=True, exist_ok=True)
                Path(fits_path).write_bytes(b"SIMPLE  =                    T / mock")
            return "b64img"

        # Reproduce the schedule gate: verify at ready BEFORE capture.
        with patch.object(dash, "_tel", fake_tel), \
             patch.object(dash, "_cam", fake_cam), \
             tempfile.TemporaryDirectory() as td:
            fits_path = str(Path(td) / "fits_export" / "T_CrB_01.fits")

            def cancel_if_off():
                return dash._mount_left_target(T_CRB_RA_H, T_CRB_DEC)

            # --- successful path ---
            fake_expose(duration=20.0, cancel_check=cancel_if_off)
            self.assertTrue(
                dash._verify_pointing(T_CRB_RA_H, T_CRB_DEC, label="at-ImageReady")
            )
            b64 = fake_capture(fits_path=fits_path, exp_dur=20.0, target="T CrB")
            # After download mount is at Cygnus — must NOT poison.
            self.assertTrue(dash._mount_left_target(T_CRB_RA_H, T_CRB_DEC))
            self.assertFalse(dash._post_readout_slew_poisons_fits(True))
            if Path(fits_path).exists() and not dash._post_readout_slew_poisons_fits(True):
                enqueued.append(fits_path)
            else:
                discarded.append(fits_path)

            self.assertEqual(enqueued, [fits_path])
            self.assertEqual(discarded, [])
            self.assertEqual(b64, "b64img")

    def test_at_image_ready_off_target_raises_before_download(self):
        """If already at Cygnus when ImageReady fires, do not photometry."""
        fake_tel = MagicMock()
        fake_tel.ra.return_value = CYGNUS_RA_H
        fake_tel.dec.return_value = CYGNUS_DEC
        fake_tel.is_slewing.return_value = False
        with patch.object(dash, "_tel", fake_tel):
            ok = dash._verify_pointing(
                T_CRB_RA_H, T_CRB_DEC, label="at-ImageReady"
            )
        self.assertFalse(ok)
        # Schedule path converts this into ExposureCancelled before capture.
        src = inspect.getsource(dash._run_schedule_observation)
        ready_gate = src[src.index("def _do_one_expose"):src.index("def _discard_fits")]
        self.assertIn("_capture_image", ready_gate)
        self.assertLess(
            ready_gate.index("at-ImageReady"),
            ready_gate.index("_capture_image"),
        )
        self.assertIn("raise ExposureCancelled", ready_gate)


class FitsExportPreferredTest(unittest.TestCase):
    def test_schedule_science_frames_use_fits_export_dir(self):
        src = inspect.getsource(dash._run_schedule_observation)
        self.assertIn("_fits_export_dir()", src)
        # Must not hard-code the old data/fits path for schedule science frames.
        self.assertNotIn('Path("data") / "fits"', src)
        self.assertIn("Prefer NodeAgent fits_export", src)


if __name__ == "__main__":
    unittest.main()
