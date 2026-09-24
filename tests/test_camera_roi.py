"""Camera.reset_roi must request NumX/NumY in binned pixels.

CameraXSize/CameraYSize are always unbinned (ASCOM/ALPACA ICameraV3), but
NumX/NumY must be given in binned pixels. Passing the unbinned sensor size
straight through after set_binning(2, 2) would ask for a subframe up to 4x
larger than the binned frame actually has.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from alpaca.camera import Camera


class ResetRoiBinningTest(unittest.TestCase):
    def setUp(self):
        self.cam = Camera("127.0.0.1", 32323)
        self.cam._c = MagicMock()

    def test_reset_roi_divides_by_current_binning(self):
        values = {"cameraxsize": 1920, "cameraysize": 1080, "binx": 2, "biny": 2}
        self.cam._c._get.side_effect = lambda attr, **k: values[attr]

        self.cam.reset_roi()

        self.cam._c._put.assert_any_call("numx", NumX=960)
        self.cam._c._put.assert_any_call("numy", NumY=540)

    def test_reset_roi_unbinned_is_a_no_op_divide(self):
        values = {"cameraxsize": 1920, "cameraysize": 1080, "binx": 1, "biny": 1}
        self.cam._c._get.side_effect = lambda attr, **k: values[attr]

        self.cam.reset_roi()

        self.cam._c._put.assert_any_call("numx", NumX=1920)
        self.cam._c._put.assert_any_call("numy", NumY=1080)


if __name__ == "__main__":
    unittest.main()
