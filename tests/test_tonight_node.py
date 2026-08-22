#!/usr/bin/env python3
"""The node's half of tonight's intent: obeying it, and failing safe when it can't.

The cloud decides whether a telescope observes (cloud/nightly.py). This is the
node acting on that decision, where two failure directions both cost something
real:

  obeying too eagerly   a node that stops observing because it could not reach
                        the cloud loses a whole clear night for nothing
  obeying too slowly    a member standing their telescope down because
                        something is wrong needs it to stop now, not after the
                        current 300-second exposure finishes

So: unknown intent means carry on, and a stand-down aborts the exposure in
flight rather than waiting for it.

Run with:  python3 -m pytest tests/test_tonight_node.py
"""

import time
import unittest
from unittest.mock import MagicMock, patch

import src.dashboard as dash
from src.cloud_communicator import CloudCommunicator


def _intent(observing=True, status="accepted", hours=None, imaging=False,
            reason=""):
    proposal = {"mode": "research"}
    if hours is not None:
        proposal["research_hours"] = hours
    proposal["imaging_after"] = imaging
    return {"node_id": "node_1", "night": "2026-08-21", "status": status,
            "observing": observing, "reason": reason, "proposal": proposal}


class _DashboardState(unittest.TestCase):
    """Each test starts with no known intent and no running schedule."""

    def setUp(self):
        with dash._tonight_lock:
            dash._tonight.clear()
        with dash._sched_lock:
            dash._sched_state["running"] = False
            dash._sched_state["cancelled"] = False
            dash._sched_state.pop("started_at", None)
        self.addCleanup(self._reset)

    def _reset(self):
        with dash._tonight_lock:
            dash._tonight.clear()
        with dash._sched_lock:
            dash._sched_state["running"] = False
            dash._sched_state["cancelled"] = False

    def _set(self, intent):
        with dash._tonight_lock:
            dash._tonight.clear()
            dash._tonight.update(intent)


class AllowsObservingTest(_DashboardState):

    def test_an_unknown_intent_allows_observing(self):
        """Never lose a night because the cloud was unreachable."""
        self.assertTrue(dash._tonight_allows_observing())

    def test_an_observing_intent_allows_observing(self):
        self._set(_intent(observing=True))
        self.assertTrue(dash._tonight_allows_observing())

    def test_a_stand_down_stops_observing(self):
        self._set(_intent(observing=False, status="stood_down"))
        self.assertFalse(dash._tonight_allows_observing())

    def test_a_weather_hold_stops_observing(self):
        self._set(_intent(observing=False, status="weather_hold"))
        self.assertFalse(dash._tonight_allows_observing())


class ResearchWindowTest(_DashboardState):

    def _running_since(self, seconds_ago: float):
        with dash._sched_lock:
            dash._sched_state["started_at"] = time.time() - seconds_ago

    def test_an_unbounded_night_never_expires(self):
        """No imaging tail means research runs until dawn."""
        self._set(_intent(hours=2, imaging=False))
        self._running_since(10 * 3600)
        self.assertFalse(dash._research_window_expired())

    def test_the_window_is_open_before_the_hours_elapse(self):
        self._set(_intent(hours=2, imaging=True))
        self._running_since(1800)          # 30 minutes in
        self.assertFalse(dash._research_window_expired())

    def test_the_window_closes_after_the_hours_elapse(self):
        self._set(_intent(hours=2, imaging=True))
        self._running_since(2 * 3600 + 60)
        self.assertTrue(dash._research_window_expired())

    def test_zero_hours_is_treated_as_unbounded_not_as_instantly_over(self):
        """Otherwise a malformed proposal would silently skip all the science."""
        self._set(_intent(hours=0, imaging=True))
        self._running_since(3600)
        self.assertFalse(dash._research_window_expired())

    def test_a_run_that_never_started_has_no_window(self):
        self._set(_intent(hours=2, imaging=True))
        self.assertFalse(dash._research_window_expired())

    def test_a_malformed_hours_value_does_not_crash(self):
        self._set(_intent(hours="soon", imaging=True))
        self._running_since(3600)
        self.assertFalse(dash._research_window_expired())


class StandDownTest(_DashboardState):

    def test_an_observing_intent_does_not_cancel_a_running_schedule(self):
        with dash._sched_lock:
            dash._sched_state["running"] = True
        dash._on_cloud_tonight(_intent(observing=True))
        with dash._sched_lock:
            self.assertFalse(dash._sched_state["cancelled"])

    def test_a_stand_down_cancels_the_running_schedule(self):
        with dash._sched_lock:
            dash._sched_state["running"] = True
        with patch.object(dash, "_cam", None), patch.object(dash, "_tel", None):
            dash._on_cloud_tonight(
                _intent(observing=False, status="stood_down",
                        reason="mount is making a noise"))
        with dash._sched_lock:
            self.assertTrue(dash._sched_state["cancelled"])

    def test_a_stand_down_aborts_the_exposure_in_flight(self):
        """Waiting out a 300 s exposure is not 'stop now'."""
        with dash._sched_lock:
            dash._sched_state["running"] = True
        cam = MagicMock()
        with patch.object(dash, "_cam", cam), patch.object(dash, "_tel", None):
            dash._on_cloud_tonight(_intent(observing=False, status="stood_down"))
        cam.abort_exposure.assert_called_once()

    def test_a_stand_down_parks_the_mount(self):
        with dash._sched_lock:
            dash._sched_state["running"] = True
        tel = MagicMock()
        with patch.object(dash, "_cam", None), patch.object(dash, "_tel", tel):
            dash._on_cloud_tonight(_intent(observing=False, status="stood_down"))
        tel.park.assert_called_once()

    def test_nothing_is_aborted_when_no_schedule_is_running(self):
        cam, tel = MagicMock(), MagicMock()
        with patch.object(dash, "_cam", cam), patch.object(dash, "_tel", tel):
            dash._on_cloud_tonight(_intent(observing=False, status="declined"))
        cam.abort_exposure.assert_not_called()
        tel.park.assert_not_called()

    def test_a_failing_abort_does_not_stop_the_park(self):
        """One broken device must not leave the mount tracking into a wall."""
        with dash._sched_lock:
            dash._sched_state["running"] = True
        cam, tel = MagicMock(), MagicMock()
        cam.abort_exposure.side_effect = RuntimeError("camera not responding")
        with patch.object(dash, "_cam", cam), patch.object(dash, "_tel", tel):
            dash._on_cloud_tonight(_intent(observing=False, status="stood_down"))
        tel.park.assert_called_once()

    def test_the_intent_is_recorded_even_when_it_stops_the_night(self):
        with patch.object(dash, "_cam", None), patch.object(dash, "_tel", None):
            dash._on_cloud_tonight(
                _intent(observing=False, status="weather_hold", reason="rain"))
        self.assertEqual(dash._tonight_intent()["status"], "weather_hold")
        self.assertFalse(dash._tonight_allows_observing())


class CommunicatorPollTest(unittest.TestCase):
    """The agent side: polling, change detection, and surviving an outage."""

    def _comm(self, on_tonight=None):
        comm = CloudCommunicator.__new__(CloudCommunicator)
        comm._on_tonight = on_tonight
        comm._tonight = {}
        import threading
        comm._tonight_lock = threading.Lock()
        return comm

    def test_observing_is_assumed_before_the_first_poll(self):
        self.assertTrue(self._comm().observing_tonight())

    def test_the_callback_fires_on_the_first_answer(self):
        seen = []
        comm = self._comm(on_tonight=seen.append)
        with patch.object(comm, "_get", return_value=_intent(observing=True)):
            comm._poll_tonight()
        self.assertEqual(len(seen), 1)
        self.assertTrue(comm.observing_tonight())

    def test_an_unchanged_answer_does_not_fire_the_callback_again(self):
        """Re-cancelling every poll would fight the scheduler."""
        seen = []
        comm = self._comm(on_tonight=seen.append)
        with patch.object(comm, "_get", return_value=_intent(observing=True)):
            comm._poll_tonight()
            comm._poll_tonight()
            comm._poll_tonight()
        self.assertEqual(len(seen), 1)

    def test_a_change_to_not_observing_fires_the_callback(self):
        seen = []
        comm = self._comm(on_tonight=seen.append)
        with patch.object(comm, "_get", return_value=_intent(observing=True)):
            comm._poll_tonight()
        with patch.object(comm, "_get",
                          return_value=_intent(observing=False,
                                               status="stood_down")):
            comm._poll_tonight()
        self.assertEqual(len(seen), 2)
        self.assertFalse(seen[-1]["observing"])
        self.assertFalse(comm.observing_tonight())

    def test_an_unreachable_cloud_keeps_the_last_known_intent(self):
        """A network blip must not cost a clear night."""
        comm = self._comm()
        with patch.object(comm, "_get", return_value=_intent(observing=True)):
            comm._poll_tonight()
        with patch.object(comm, "_get", side_effect=RuntimeError("connection reset")):
            comm._poll_tonight()
        self.assertTrue(comm.observing_tonight())
        self.assertEqual(comm.active_tonight()["status"], "accepted")

    def test_a_nonsense_response_is_ignored(self):
        comm = self._comm()
        with patch.object(comm, "_get", return_value={"unexpected": True}):
            comm._poll_tonight()
        self.assertTrue(comm.observing_tonight())
        self.assertEqual(comm.active_tonight(), {})

    def test_a_raising_callback_does_not_break_polling(self):
        comm = self._comm(on_tonight=MagicMock(side_effect=RuntimeError("boom")))
        with patch.object(comm, "_get", return_value=_intent(observing=False)):
            comm._poll_tonight()          # must not raise
        self.assertFalse(comm.observing_tonight())


if __name__ == "__main__":
    unittest.main()
