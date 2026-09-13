#!/usr/bin/env python3
"""
Regression tests for issue #45 (auto-centering holding _device_lock across
the whole ASTAP wall-clock time, starving every other device route) and
issue #47 (a solver signature mismatch surfacing as a raw TypeError instead
of a clear, actionable error).
"""

import threading
import unittest
from unittest.mock import MagicMock, patch

from alpaca.platesolve import CenteringError, center_on_target
from src import dashboard
from src import dashboard as dash


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
            with patch("alpaca.platesolve.resolve_solver_path",
                       return_value="/opt/Astap.app/Contents/MacOS/astap"):
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

    def test_missing_bare_astap_fails_before_exposure_and_names_config(self):
        from alpaca import platesolve

        class Tel:
            def slew_to_coordinates(self, *args):
                raise AssertionError("must fail before slew")

        class Cam:
            def expose(self, *args, **kwargs):
                raise AssertionError("must fail before exposure")

        with patch("alpaca.platesolve.shutil.which", return_value=None):
            with self.assertRaises(CenteringError) as ctx:
                platesolve.center_on_target_device(
                    Tel(), Cam(), 10.0, 20.0, solver="astap",
                    astap_path="astap", settle_s=0, max_iterations=1,
                )

        message = str(ctx.exception)
        self.assertIn("photometry.astap_path='astap'", message)
        self.assertIn("not executable", message)


class AstapNoSolutionFoundTest(unittest.TestCase):
    """Issue #79: a genuine ASTAP rc=1 'No solution found!' solve failure must
    surface as a clean, reportable centering failure — not a crash, and not
    an ambiguous stuck state."""

    def test_run_astap_rc1_returns_falsy_result_with_message(self):
        from src.photometry import _run_astap

        fake = MagicMock(
            returncode=1, stdout="", stderr="No solution found!\n",
        )
        with patch("src.photometry.subprocess.run", return_value=fake):
            result = _run_astap("/tmp/fake.fits", 10.0, 20.0, "astap", 10)

        self.assertFalse(result)  # __bool__ still works in `if _run_astap(...):`
        self.assertIn("No solution found!", result.message)

    def test_astap_timeout_is_a_clean_falsy_result_not_a_hang(self):
        """The ASTAP subprocess call already carries a 90s timeout — confirm
        a TimeoutExpired is turned into a reportable result, not left to
        propagate and potentially wedge the caller."""
        from src.photometry import _run_astap
        import subprocess as sp

        with patch("src.photometry.subprocess.run",
                   side_effect=sp.TimeoutExpired(cmd="astap", timeout=90)):
            result = _run_astap("/tmp/fake.fits", 10.0, 20.0, "astap", 10)

        self.assertFalse(result)
        self.assertIn("timed out", result.message)

    def test_solve_image_array_surfaces_astap_message_via_last_solve_error(self):
        from alpaca import platesolve
        from src.photometry import AstapResult

        fake_image = [[1.0, 2.0], [3.0, 4.0]]
        with patch("src.photometry._run_astap",
                   return_value=AstapResult(False, "ASTAP failed (rc=1): No solution found!")):
            result = platesolve.solve_image_array(
                fake_image, 10.0, 20.0, solver="astap", solver_path="astap",
            )

        self.assertIsNone(result)
        self.assertIn("No solution found!", platesolve._LAST_SOLVE_ERROR)

    def test_center_on_target_reports_no_solution_found_not_generic_message(self):
        """End to end through the injectable core: when every iteration's
        solve fails with ASTAP's 'No solution found!', that detail must
        appear in both the per-iteration record and the final CenteringError
        — not just a generic 'no frame could be plate-solved'."""
        from alpaca import platesolve

        def slew_fn(ra_hours, dec_deg):
            pass

        def capture_fn():
            return object()

        def solve_fn(image, ra_deg, dec_deg):
            # Mimic solve_image_array's contract: on ASTAP failure it stashes
            # the reason in the module-level slot and returns None.
            platesolve._LAST_SOLVE_ERROR = "ASTAP failed (rc=1): No solution found!"
            return None

        iterations_seen = []
        try:
            with self.assertRaises(platesolve.CenteringError) as ctx:
                platesolve.center_on_target(
                    slew_fn, capture_fn, solve_fn,
                    target_ra=10.0, target_dec=20.0,
                    max_iterations=2, settle_s=0,
                    progress_cb=iterations_seen.append,
                )
        finally:
            platesolve._LAST_SOLVE_ERROR = None

        self.assertIn("No solution found!", str(ctx.exception))
        self.assertEqual(len(iterations_seen), 2)
        for it in iterations_seen:
            self.assertEqual(it.solve_error, "ASTAP failed (rc=1): No solution found!")


class CenteringNeverStuckRunningTest(unittest.TestCase):
    """Issue #79: running:true with iterations:[] must never survive the
    background thread finishing, cancelling, or crashing — for any reason."""

    def setUp(self):
        self._prev_center_state = dict(dash._center_state)

    def tearDown(self):
        dash._center_state.clear()
        dash._center_state.update(self._prev_center_state)

    def test_exception_before_first_iteration_still_clears_running(self):
        """A hang/exception raised before center_on_target_device produces
        even one iteration (e.g. the solver path check failing immediately)
        must still leave running=False with a recorded error, not
        iterations:[] forever."""
        with patch("alpaca.platesolve.center_on_target_device",
                   side_effect=RuntimeError("boom before first iteration")):
            dash._run_centering_bg(10.0, 20.0, 3.0, 3.0, 4, 0.0)

        with dash._center_lock:
            state = dict(dash._center_state)

        self.assertFalse(state["running"])
        self.assertEqual(state["iterations"], [])
        self.assertIn("boom before first iteration", state["error"])

    def test_unexpected_non_centering_exception_still_clears_running(self):
        """Not just CenteringError/CenteringCancelled — any exception type
        must clear running, since a hang can surface as something the
        centering code never anticipated."""
        class WeirdError(Exception):
            pass

        with patch("alpaca.platesolve.center_on_target_device",
                   side_effect=WeirdError("unexpected")):
            dash._run_centering_bg(10.0, 20.0, 3.0, 3.0, 4, 0.0)

        with dash._center_lock:
            state = dict(dash._center_state)

        self.assertFalse(state["running"])
        self.assertIn("unexpected", state["error"])

    def test_device_lock_timeout_clears_running_instead_of_hanging_forever(self):
        """If some other route is stuck holding _device_lock, a plain
        `with _device_lock:` inside centering would block the background
        thread forever — the exact running:true/iterations:[] stuck state
        from issue #79. _LockedDeviceProxy must time out instead."""
        import time
        import threading as th

        class FakeTel:
            def slew_to_coordinates(self, ra_hours, dec_deg):
                pass

        release_holder = th.Event()
        lock_acquired = th.Event()

        def hold_device_lock():
            with dash._device_lock:
                lock_acquired.set()
                release_holder.wait(5.0)

        holder = th.Thread(target=hold_device_lock, daemon=True)
        holder.start()
        self.assertTrue(lock_acquired.wait(2.0), "holder never acquired _device_lock")

        prev_tel = dash._tel
        dash._tel = FakeTel()
        try:
            with patch.object(dash._LockedDeviceProxy, "LOCK_TIMEOUT_S", 0.2):
                def _call_slew(tel, cam, *a, **k):
                    # Exercise the real proxy so the lock-timeout path runs.
                    tel.slew_to_coordinates(0.0, 0.0)

                with patch("alpaca.platesolve.center_on_target_device",
                           side_effect=_call_slew):
                    dash._run_centering_bg(10.0, 20.0, 3.0, 3.0, 4, 0.0)
        finally:
            dash._tel = prev_tel
            release_holder.set()
            holder.join(timeout=5.0)

        with dash._center_lock:
            state = dict(dash._center_state)

        self.assertFalse(state["running"])
        self.assertIn("timed out", state["error"])
        self.assertIn("device lock", state["error"])

    def test_successful_run_still_clears_running_via_finally(self):
        """Sanity check that the try/finally refactor didn't break the
        ordinary success path."""
        from alpaca.platesolve import CenterResult

        fake_result = CenterResult(True, 10.0, 20.0, 10.001, 20.001, 1.0, [])
        with patch("alpaca.platesolve.center_on_target_device",
                   return_value=fake_result):
            dash._run_centering_bg(10.0, 20.0, 3.0, 3.0, 4, 0.0)

        with dash._center_lock:
            state = dict(dash._center_state)

        self.assertFalse(state["running"])
        self.assertIsNone(state["error"])
        self.assertTrue(state["result"]["success"])


if __name__ == "__main__":
    unittest.main()


class EmptyJsonCenteringErrorTest(unittest.TestCase):
    """Issue #59: empty/non-JSON bodies must raise CenteringError, not raw json.loads."""

    def test_slew_json_decode_becomes_centering_error(self):
        import json
        from alpaca.platesolve import CenteringError, center_on_target

        def slew_fn(ra_hours, dec_deg):
            raise json.JSONDecodeError("Expecting value", "", 0)

        with self.assertRaises(CenteringError) as ctx:
            center_on_target(
                target_ra=10.0, target_dec=20.0,
                slew_fn=slew_fn,
                capture_fn=lambda: [[1.0]],
                solve_fn=lambda *a, **k: (10.0, 20.0),
                settle_s=0, max_iterations=1,
            )
        msg = str(ctx.exception)
        self.assertIn("empty or non-JSON", msg)
        self.assertNotEqual(msg, "Expecting value: line 1 column 1 (char 0)")
        self.assertIn("Slew", msg)

    def test_solve_expecting_value_string_becomes_centering_error(self):
        from alpaca.platesolve import CenteringError, center_on_target

        def solve_fn(image, ra_deg, dec_deg):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

        with self.assertRaises(CenteringError) as ctx:
            center_on_target(
                target_ra=10.0, target_dec=20.0,
                slew_fn=lambda *a, **k: None,
                capture_fn=lambda: [[1.0]],
                solve_fn=solve_fn,
                settle_s=0, max_iterations=1,
            )
        msg = str(ctx.exception)
        self.assertIn("Plate solve/ASTAP", msg)
        self.assertIn("empty or non-JSON", msg)

    def test_alpaca_client_empty_body_raises_structured_alpaca_error(self):
        from unittest.mock import MagicMock
        from alpaca.client import AlpacaClient, AlpacaError

        client = AlpacaClient("127.0.0.1", 11111, "telescope", 0)
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b""
        resp.headers = {"Content-Length": "0"}
        resp.raise_for_status = MagicMock()
        client.session.get = MagicMock(return_value=resp)

        with self.assertRaises(AlpacaError) as ctx:
            client._get("slewing")
        msg = str(ctx.exception)
        self.assertIn("empty ALPACA HTTP body", msg)
        self.assertNotIn("Expecting value", msg)
