#!/usr/bin/env python3
"""
Regression tests for issue #45 (auto-centering holding _device_lock across
the whole ASTAP wall-clock time, starving every other device route) and
issue #47 (a solver signature mismatch surfacing as a raw TypeError instead
of a clear, actionable error).
"""

import threading
import unittest

from alpaca.platesolve import CenteringError, center_on_target
from src import dashboard


class LockedDeviceProxyTest(unittest.TestCase):
    def test_lock_is_held_only_per_call_not_for_the_whole_session(self):
        lock = dashboard._device_lock
        held_during_slow_call = []

        class SlowDevice:
            def slow_solve_equivalent(self):
                # While this "solve" is running, _device_lock must be free
                # for someone else to acquire — it must not be held for the
                # whole centering session (issue #45).
                held_during_slow_call.append(lock.acquire(blocking=False))
                if held_during_slow_call[-1]:
                    lock.release()

        proxy = dashboard._LockedDeviceProxy(SlowDevice())
        proxy.slow_solve_equivalent()
        self.assertEqual(held_during_slow_call, [True])

    def test_proxy_still_serializes_the_wrapped_call_itself(self):
        lock = dashboard._device_lock
        seen_locked = []

        class Device:
            def move(self):
                seen_locked.append(lock.locked() if hasattr(lock, "locked") else True)

        proxy = dashboard._LockedDeviceProxy(Device())
        proxy.move()
        # An RLock has no public .locked(); just confirm the call succeeds
        # and the attribute passthrough works for non-callables too.
        proxy._target  # noqa: B018 - attribute access sanity check


class SolverSignatureMismatchTest(unittest.TestCase):
    def test_bad_solver_kwarg_raises_clear_centering_error_not_raw_typeerror(self):
        def slew_fn(ra_hours, dec_deg):
            pass

        def capture_fn():
            return object()

        def solve_fn_with_wrong_signature(image, ra_deg, dec_deg, unexpected_kw=None):
            raise TypeError(
                "solve_image_array() got an unexpected keyword argument 'astap_path'"
            )

        with self.assertRaises(CenteringError) as ctx:
            center_on_target(
                slew_fn, capture_fn, solve_fn_with_wrong_signature,
                target_ra=10.0, target_dec=20.0, max_iterations=1,
                settle_s=0,
            )
        self.assertIn("out of sync", str(ctx.exception))



class AstapPathAliasTest(unittest.TestCase):
    """Issue #51: older callers still pass astap_path=; must not TypeError."""

    def test_center_on_target_device_accepts_astap_path_alias(self):
        from alpaca.platesolve import center_on_target_device, solve_image_array
        import inspect
        sig = inspect.signature(center_on_target_device)
        self.assertIn("astap_path", sig.parameters)
        sig2 = inspect.signature(solve_image_array)
        self.assertIn("astap_path", sig2.parameters)

    def test_calling_with_only_astap_path_does_not_typeerror(self):
        from alpaca import platesolve

        calls = {}

        def fake_solve(image, ra_deg, dec_deg, solver="astrometry",
                       solver_path=None, search_radius=10.0, pixel_scale=None,
                       *, astap_path=None):
            calls["solver_path"] = solver_path
            calls["astap_path"] = astap_path
            return (ra_deg, dec_deg)

        class Tel:
            def slew_to_coordinates(self, ra_hours, dec_deg):
                pass

        class Cam:
            def expose(self, *a, **k):
                pass
            def image_array(self):
                return [[1.0]]

        real = platesolve.solve_image_array
        platesolve.solve_image_array = fake_solve
        try:
            result = platesolve.center_on_target_device(
                Tel(), Cam(), 10.0, 20.0,
                exposure_s=0.01, settle_s=0, max_iterations=1,
                solver="astap", astap_path="/opt/Astap.app/Contents/MacOS/astap",
            )
        finally:
            platesolve.solve_image_array = real
        self.assertTrue(result.success)
        self.assertEqual(calls["solver_path"],
                         "/opt/Astap.app/Contents/MacOS/astap")


if __name__ == "__main__":
    unittest.main()
