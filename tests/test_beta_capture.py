"""scripts/beta_capture.py manifest bookkeeping.

Adding the identical file bytes under a second campaign must not evict the
first campaign's manifest entry -- each campaign gets its own on-disk copy
(destination path includes the campaign code), so dedup has to be scoped to
(sha256, campaign), not sha256 alone.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from astropy.io import fits

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "beta_capture", REPO / "scripts" / "beta_capture.py")
beta_capture = importlib.util.module_from_spec(_spec)
sys.modules["beta_capture"] = _spec.loader.exec_module(beta_capture) or beta_capture


class CrossCampaignDedupTest(unittest.TestCase):
    def test_same_file_under_two_campaigns_keeps_both_manifest_entries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "corpus"
            src = Path(td) / "frame.fits"
            hdu = fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16))
            hdu.writeto(src)

            beta_capture.add(root, "C1", src, "node042", None, None)
            beta_capture.add(root, "C4", src, "node042", None, None)

            manifest = beta_capture._manifest(root)
            campaigns = sorted(c["campaign"] for c in manifest["captures"])
            self.assertEqual(campaigns, ["C1", "C4"],
                             "both campaigns' entries must survive")

            # Both physical copies still exist and are still verified.
            self.assertEqual(beta_capture.audit(root), 0)


if __name__ == "__main__":
    unittest.main()
