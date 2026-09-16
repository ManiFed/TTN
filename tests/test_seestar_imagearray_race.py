"""Seestar ImageReady/imagearray race (Starfront 2026-09-16).

ImageReady can flip true while CameraState is still EXPOSING (2); the first
imagearray call then returns AlpacaError "no image available". expose() must
keep waiting in that window, and image_array() must retry briefly.
"""

from __future__ import annotations

import itertools
import unittest
from unittest.mock import MagicMock, patch

from alpaca.camera import Camera, _STATE_DOWNLOAD, _STATE_EXPOSING, _STATE_IDLE
from alpaca.client import AlpacaError


class SeestarImageReadyRaceTest(unittest.TestCase):
    def test_expose_waits_through_exposing_with_imageready(self):
        cam = Camera("127.0.0.1", 32323)
        states = [_STATE_EXPOSING, _STATE_EXPOSING, _STATE_DOWNLOAD]
        ready = [True, True, True]

        cam._c = MagicMock()
        cam._c._put = MagicMock()
        cam.camera_state = MagicMock(side_effect=states)
        cam.image_ready = MagicMock(side_effect=ready)

        # expose() polls monotonic for deadline + each loop guard + idle check;
        # a short finite side_effect list raises StopIteration mid-poll.
        clock = itertools.count(start=0, step=1)

        with patch("alpaca.camera.time.sleep", return_value=None), \
             patch("alpaca.camera.time.monotonic", side_effect=lambda: next(clock)):
            cam.expose(duration=0.1, readout_timeout=5.0)

        self.assertEqual(cam.camera_state.call_count, 3)

    def test_image_array_retries_no_image_available(self):
        cam = Camera("127.0.0.1", 32323)
        cam._c = MagicMock()
        calls = [
            AlpacaError("imagearray → ErrorNumber 1: no image available", code=1),
            [[1, 2], [3, 4]],
        ]

        def _get(attr, timeout=10, **params):
            assert attr == "imagearray"
            item = calls.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        cam._c._get.side_effect = _get
        clock = itertools.count(start=0, step=1)
        with patch("alpaca.camera.time.sleep", return_value=None), \
             patch("alpaca.camera.time.monotonic", side_effect=lambda: next(clock)):
            data = cam.image_array(timeout=30.0)
        self.assertEqual(data, [[1, 2], [3, 4]])
        self.assertEqual(cam._c._get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
