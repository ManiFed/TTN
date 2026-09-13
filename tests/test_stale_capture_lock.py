#!/usr/bin/env python3
"""
Regression tests for issue #95 — a stale "capture is active" lock could block
every later expose until a human ran node_abort_exposure + node_schedule_abort
and restarted the NodeAgent process.

POST /api/camera/expose reserves a latch (``_state["camera"]["exposing"]``)
before a background worker thread talks to the camera.  If that worker never
reaches its ``finally`` (e.g. a device call hangs past its own timeout), the
latch used to stay set forever.  These tests cover:

  * a lock held far longer than any real capture could take is detected as
    stale and cleared automatically, aborting the underlying camera;
  * a subsequent expose then proceeds without a restart;
  * a genuinely fresh/healthy lock is left alone (not falsely aborted);
  * a stale worker thread that eventually wakes up cannot clobber the state
    of the exposure that reclaimed its lock ("generation" guard);
  * a failed/aborted frame is never counted as a completed capture.
"""

import pathlib
import tempfile
import threading
import time
import unittest

from src import dashboard


class _FakeCam:
    """Minimal camera stand-in for exercising api_expose()'s worker thread."""

    def __init__(self, expose_fn=None):
        self.abort_calls = 0
        self.set_binning_calls = 0
        self._expose_fn = expose_fn or (lambda: None)

    def set_binning(self, *_a, **_k):
        self.set_binning_calls += 1

    def expose(self, *_a, **_k):
        self._expose_fn()

    def abort_exposure(self):
        self.abort_calls += 1

    def ccd_temperature(self):
        return -10.0

    def image_array(self):
        return [[1.0]]


def _fake_capture_image(fits_path=None, exp_dur=None, target=None):
    """Stand-in for dashboard._capture_image that skips numpy/astropy/PIL.

    Writes a placeholder file at fits_path (mirroring a successful FITS
    write) and returns a syntactically valid base64 PNG placeholder.
    """
    if fits_path:
        pathlib.Path(fits_path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(fits_path).write_text("fake-fits")
    return "ZmFrZQ=="  # base64("fake")


class StaleCaptureLockTest(unittest.TestCase):
    def setUp(self):
        dashboard.app.testing = True
        self.client = dashboard.app.test_client()

        self._prev_cam = dashboard._cam
        self._prev_capture_image = dashboard._capture_image
        self._prev_enqueue = dashboard._enqueue_photometry
        self._prev_camera_state = dict(dashboard._state["camera"])
        self._prev_image_captured = dashboard._state["image_captured"]

        dashboard._capture_image = _fake_capture_image
        self._enqueue_calls = []
        dashboard._enqueue_photometry = lambda *a, **k: self._enqueue_calls.append((a, k))

        # Exposures write FITS under _load_config()'s photometry.fits_export
        # dir (default "fits_export", relative to the repo root) — redirect
        # that to a throwaway temp dir so tests don't litter the checkout.
        self._prev_load_config = dashboard._load_config
        self._tmp_export_dir = tempfile.TemporaryDirectory()
        dashboard._load_config = lambda: {
            "photometry": {"fits_export": {"export_dir": self._tmp_export_dir.name}},
        }

        # Reset to a clean, idle latch before each test.
        with dashboard._state_lock:
            dashboard._state["camera"].update({
                "exposing": False,
                "exposure_start_ts": None,
                "exposure_duration": None,
                "exposure_readout_timeout": None,
                "exposure_generation": 0,
                "error": None,
            })
            dashboard._state["image_captured"] = False

    def tearDown(self):
        dashboard._cam = self._prev_cam
        dashboard._capture_image = self._prev_capture_image
        dashboard._enqueue_photometry = self._prev_enqueue
        dashboard._load_config = self._prev_load_config
        self._tmp_export_dir.cleanup()
        with dashboard._state_lock:
            dashboard._state["camera"].update(self._prev_camera_state)
            dashboard._state["image_captured"] = self._prev_image_captured
        dashboard._expose_cancel.clear()

    def _wait_until_idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with dashboard._state_lock:
                if not dashboard._state["camera"]["exposing"]:
                    return True
            time.sleep(0.02)
        return False

    # -- staleness predicate itself -----------------------------------------

    def test_fresh_lock_is_not_stale(self):
        cam_state = {
            "exposure_start_ts": time.time(),
            "exposure_duration": 30.0,
            "exposure_readout_timeout": 20.0,
        }
        self.assertFalse(dashboard._exposure_lock_is_stale(cam_state))

    def test_old_lock_past_budget_plus_grace_is_stale(self):
        cam_state = {
            "exposure_start_ts": time.time() - 1000.0,
            "exposure_duration": 30.0,
            "exposure_readout_timeout": 20.0,
        }
        self.assertTrue(dashboard._exposure_lock_is_stale(cam_state))

    def test_lock_with_no_timestamp_is_treated_as_stale(self):
        # exposing=True with no start_ts is an inconsistent state that must
        # never permanently block the camera.
        self.assertTrue(dashboard._exposure_lock_is_stale({}))

    # -- HTTP behavior --------------------------------------------------------

    def test_healthy_in_progress_capture_is_rejected_not_aborted(self):
        fake_cam = _FakeCam()
        dashboard._cam = fake_cam
        with dashboard._state_lock:
            dashboard._state["camera"]["exposing"] = True
            dashboard._state["camera"]["exposure_start_ts"] = time.time()
            dashboard._state["camera"]["exposure_duration"] = 30.0
            dashboard._state["camera"]["exposure_readout_timeout"] = 20.0

        resp = self.client.post("/api/camera/expose", json={"duration": 5})

        self.assertEqual(resp.status_code, 409)
        self.assertIn("already in progress", resp.get_json()["error"])
        self.assertEqual(fake_cam.abort_calls, 0,
                          "a genuinely active capture must not be aborted")

    def test_stale_lock_is_cleared_and_next_expose_succeeds(self):
        fake_cam = _FakeCam()
        dashboard._cam = fake_cam

        # Simulate a worker thread that set the latch a long time ago and
        # never reached its `finally` (e.g. a hung device call).
        with dashboard._state_lock:
            dashboard._state["camera"]["exposing"] = True
            dashboard._state["camera"]["exposure_start_ts"] = time.time() - 1000.0
            dashboard._state["camera"]["exposure_duration"] = 5.0
            dashboard._state["camera"]["exposure_readout_timeout"] = 20.0
            dashboard._state["camera"]["exposure_generation"] = 1

        resp = self.client.post("/api/camera/expose", json={"duration": 1})

        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(fake_cam.abort_calls, 1,
                          "stale lock recovery must abort the underlying capture")

        self.assertTrue(self._wait_until_idle(),
                         "new exposure never completed — is the latch still stuck?")

        with dashboard._state_lock:
            self.assertTrue(dashboard._state["image_captured"])
            self.assertGreater(dashboard._state["camera"]["exposure_generation"], 1)
        self.assertEqual(len(self._enqueue_calls), 1,
                          "the new, successful frame should still be enqueued")

    def test_stale_worker_thread_cannot_clobber_newer_exposure(self):
        """A worker thread whose lock was reclaimed as stale must not stomp
        on the exposure that reclaimed it once it finally reaches its own
        (delayed) cleanup.

        The old worker is allowed to finish talking to the camera (so this
        does not depend on ever forcibly preempting real hardware access,
        which the fix does not attempt) — it is delayed afterwards, in its
        photometry-enqueue step, standing in for whatever slow bookkeeping
        might separate "device work done" from "thread reaches `finally`".
        """
        release_old = threading.Event()
        enqueue_calls = []

        def blocking_enqueue(*_a, **_k):
            enqueue_calls.append(1)
            if len(enqueue_calls) == 1:
                # The "old" exposure's worker parks here — past all of its
                # device/_device_lock access — until the test releases it.
                release_old.wait(timeout=5.0)

        dashboard._enqueue_photometry = blocking_enqueue

        fake_cam = _FakeCam()
        dashboard._cam = fake_cam

        # Kick off the "old" exposure. It runs its (fast, fake) device work
        # to completion and then blocks in the enqueue step above, so it
        # still holds the "exposing" latch but no longer holds _device_lock.
        resp = self.client.post("/api/camera/expose", json={"duration": 1})
        self.assertEqual(resp.status_code, 200, resp.get_json())

        deadline = time.time() + 5.0
        while time.time() < deadline and len(enqueue_calls) < 1:
            time.sleep(0.01)
        self.assertGreaterEqual(len(enqueue_calls), 1, "old exposure never reached enqueue")

        with dashboard._state_lock:
            old_generation = dashboard._state["camera"]["exposure_generation"]
            self.assertTrue(dashboard._state["camera"]["exposing"])
            # Rewind the clock so the next request sees this latch as stale.
            dashboard._state["camera"]["exposure_start_ts"] = time.time() - 1000.0

        # Second exposure should reclaim the lock as stale and run its own
        # capture to completion (device access is free — the old thread let
        # go of _device_lock before parking above).
        resp2 = self.client.post("/api/camera/expose", json={"duration": 1})
        self.assertEqual(resp2.status_code, 200, resp2.get_json())
        self.assertTrue(self._wait_until_idle(),
                         "reclaiming exposure never completed")

        with dashboard._state_lock:
            new_generation = dashboard._state["camera"]["exposure_generation"]
            self.assertGreater(new_generation, old_generation)
            self.assertFalse(dashboard._state["camera"]["exposing"])
            self.assertTrue(dashboard._state["image_captured"])

        # Now let the old, parked thread's enqueue call return and reach its
        # own `finally` block. It must NOT clobber the newer generation's
        # state (issue #95's generation guard).
        release_old.set()
        deadline = time.time() + 5.0
        while time.time() < deadline and len(enqueue_calls) < 2:
            time.sleep(0.01)
        time.sleep(0.3)  # give the old thread's finally a moment to run

        with dashboard._state_lock:
            self.assertEqual(
                dashboard._state["camera"]["exposure_generation"], new_generation,
                "a stale worker's finally must not touch a newer generation")
            self.assertFalse(
                dashboard._state["camera"]["exposing"],
                "a stale worker's finally must not re-arm the latch")

    def test_failed_frame_is_not_counted_as_captured(self):
        def boom():
            raise RuntimeError("simulated device fault")

        fake_cam = _FakeCam(expose_fn=boom)
        dashboard._cam = fake_cam

        resp = self.client.post("/api/camera/expose", json={"duration": 1})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(self._wait_until_idle(), "failed exposure never unwound the latch")

        with dashboard._state_lock:
            self.assertFalse(dashboard._state["image_captured"])
            self.assertIsNotNone(dashboard._state["camera"]["error"])
        self.assertEqual(self._enqueue_calls, [],
                          "a failed frame must never be enqueued for photometry")

    def test_cancelled_frame_is_not_counted_as_captured(self):
        def cancelled():
            dashboard._expose_cancel.set()

        fake_cam = _FakeCam(expose_fn=cancelled)
        dashboard._cam = fake_cam

        # The real Camera.expose() raises ExposureCancelled itself when
        # cancel_check() is True; our fake camera simply sets the flag, so
        # emulate that by having the fake raise it directly instead.
        def raise_cancelled():
            from alpaca.camera import ExposureCancelled
            raise ExposureCancelled("cancelled")

        fake_cam._expose_fn = raise_cancelled

        resp = self.client.post("/api/camera/expose", json={"duration": 1})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(self._wait_until_idle(), "cancelled exposure never unwound the latch")

        with dashboard._state_lock:
            self.assertFalse(dashboard._state["image_captured"])
        self.assertEqual(self._enqueue_calls, [],
                          "a cancelled frame must never be enqueued for photometry")


if __name__ == "__main__":
    unittest.main()
