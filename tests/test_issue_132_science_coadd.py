#!/usr/bin/env python3
"""Issue #132: live stack science coadd → fits_export + photometry enqueue."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from src.stacking import LiveStacker
import src.dashboard as dash


class LiveStackerWriteFitsTest(unittest.TestCase):
    def test_write_fits_creates_coadd(self):
        st = LiveStacker()
        # Directly seed accumulator — star detection on tiny synthetic fields
        # is brittle; write_fits only needs frames_stacked > 0.
        frame = np.zeros((64, 64), dtype=np.float32)
        frame[20:25, 20:25] = 1000.0
        st._accum = frame.astype(np.float64)
        st.frames_stacked = 3
        st.frames_total = 3
        self.assertGreaterEqual(st.frames_stacked, 1)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "coadd.fits"
            ok = st.write_fits(str(out), header_cards={"OBJECT": "SS Cyg"})
            self.assertTrue(ok)
            self.assertTrue(out.is_file())
            from astropy.io import fits
            with fits.open(out) as hdul:
                self.assertEqual(hdul[0].header.get("OBJECT"), "SS Cyg")
                self.assertGreaterEqual(int(hdul[0].header.get("STACKN", 0)), 1)
                self.assertEqual(hdul[0].data.shape, (64, 64))


class StackExportApiContractTest(unittest.TestCase):
    def test_export_helper_and_routes_exist(self):
        self.assertTrue(hasattr(dash, "_export_live_stack_science"))
        self.assertTrue(hasattr(dash, "api_stack_export"))
        src = inspect.getsource(dash.api_stack_start)
        self.assertIn("export_science", src)
        self.assertIn("enqueue_photometry", src)
        src2 = inspect.getsource(dash._run_stacking_bg)
        self.assertIn("export_science", src2)
        self.assertIn("_export_live_stack_science", src2)

    def test_export_helper_writes_and_enqueues(self):
        st = LiveStacker()
        frame = np.zeros((32, 32), dtype=np.float32)
        frame[10:14, 10:14] = 500.0
        st._accum = frame.astype(np.float64)
        st.frames_stacked = 4
        st.frames_total = 4
        enqueued = []
        with tempfile.TemporaryDirectory() as td:
            with patch.object(dash, "_fits_export_dir", return_value=td), \
                 patch.object(dash, "_fits_export_night_utc", return_value="2026-09-17"), \
                 patch.object(dash, "_commanded_slew_radec", return_value=(21.71, 43.58)), \
                 patch.object(dash, "_enqueue_photometry",
                              side_effect=lambda *a, **k: enqueued.append((a, k))):
                path = dash._export_live_stack_science(
                    st, target_name="SS Cyg", enqueue=True,
                )
            self.assertIsNotNone(path)
            self.assertTrue(Path(path).is_file())
            self.assertEqual(len(enqueued), 1)
            self.assertIn("SS Cyg", str(enqueued[0]))


if __name__ == "__main__":
    unittest.main()
