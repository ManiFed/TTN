#!/usr/bin/env python3
"""The imaging half of the night has to happen without anyone awake for it.

"Observe for a couple of hours of research, then put together a nice imaging
programme" was only half true: the research block ended, logged that the rest
of the night was "released for imaging", and stopped. Nothing was listening. A
member who asked for a picture got a parked telescope and no image to look at
in the morning.

The handover is the part nobody can supervise, so the tests care about the
things that only bite at 3am: that it never points somewhere the research half
could not, that a stand-down stops it as fast as it stops a research run, and
that it degrades rather than abandoning the night.

Run with:  python3 -m pytest tests/test_imaging_handoff.py
"""

import unittest
from unittest.mock import MagicMock, patch

import src.dashboard as dash


class TargetChoiceTest(unittest.TestCase):

    def test_the_catalogue_actually_yields_imaging_targets(self):
        """pyongc spells types out in full -- 'Planetary Nebula', not 'PN'.
        Guessing the abbreviations produced a catalogue of zero."""
        usable = [o for o in dash._dso_catalog
                  if o.get("type") in dash._IMAGING_TYPES]
        self.assertGreater(len(usable), 1000)

    def test_recognisable_objects_are_preferred(self):
        """A member should get M42, not an anonymous NGC galaxy."""
        usable = [o for o in dash._dso_catalog
                  if o.get("type") in dash._IMAGING_TYPES]
        top = sorted(usable, key=dash._imaging_rank)[:10]
        self.assertTrue(all(o["id"].startswith("M") and o["id"][1:].isdigit()
                            for o in top),
                        f"expected Messier objects first, got {[o['id'] for o in top]}")

    def test_nebulae_outrank_the_nine_thousand_galaxies(self):
        self.assertLess(dash._IMAGING_TYPES.index("Planetary Nebula"),
                        dash._IMAGING_TYPES.index("Galaxy"))

    def test_it_picks_a_target_that_passes_the_safety_check(self):
        with patch.object(dash, "_slew_rejection", return_value=None):
            target = dash._pick_imaging_target()
        self.assertIsNotNone(target)
        self.assertIn(target["type"], dash._IMAGING_TYPES)

    def test_it_returns_nothing_when_the_whole_sky_is_refused(self):
        """Better to skip the night than to slew somewhere unsafe."""
        with patch.object(dash, "_slew_rejection", return_value="unsafe"):
            self.assertIsNone(dash._pick_imaging_target())

    def test_it_skips_refused_targets_and_keeps_looking(self):
        calls = {"n": 0}

        def reject_first_three(ra, dec):
            calls["n"] += 1
            return "below horizon" if calls["n"] <= 3 else None

        with patch.object(dash, "_slew_rejection", side_effect=reject_first_three):
            target = dash._pick_imaging_target()
        self.assertIsNotNone(target)
        self.assertEqual(calls["n"], 4)


class ImagingBlockTest(unittest.TestCase):

    def setUp(self):
        with dash._imaging_lock:
            dash._imaging_state.update({"running": False, "target": "",
                                        "started_at": None, "error": None})
        with dash._sched_lock:
            dash._sched_state["cancelled"] = False
        with dash._tonight_lock:
            dash._tonight.clear()
        self.addCleanup(self._reset)

    def _reset(self):
        with dash._imaging_lock:
            dash._imaging_state["running"] = False
        with dash._sched_lock:
            dash._sched_state["cancelled"] = False
        with dash._tonight_lock:
            dash._tonight.clear()

    TARGET = {"id": "M42", "type": "Nebula", "ra": 5.588, "dec": -5.39}

    def _run(self, **overrides):
        """Run the block with the mount stubbed and the hold loop short."""
        tel = overrides.pop("tel", MagicMock())
        stack = overrides.pop("stack", MagicMock())
        # Stop the hold loop immediately: the block exits when the schedule is
        # cancelled, which is also how a stand-down stops it.
        with dash._sched_lock:
            dash._sched_state["cancelled"] = True
        with patch.object(dash, "_tel", tel), \
             patch.object(dash, "_slew_rejection", return_value=None), \
             patch.object(dash, "_run_centering_bg", MagicMock()), \
             patch.object(dash, "_run_stacking_bg", stack), \
             patch.object(dash, "_safety_mgr", None), \
             patch("threading.Thread", MagicMock()):
            dash._run_imaging_block(self.TARGET)
        return tel, stack

    def test_it_slews_in_hours_not_degrees(self):
        """slew_to_coordinates takes RA in hours; _run_centering_bg takes
        degrees. Confusing them points the mount fifteen times too far."""
        tel, _ = self._run()
        tel.slew_to_coordinates.assert_called_once()
        ra_arg = tel.slew_to_coordinates.call_args[0][0]
        self.assertAlmostEqual(ra_arg, 5.588, places=3)

    def test_it_centres_in_degrees(self):
        with dash._sched_lock:
            dash._sched_state["cancelled"] = True
        centering = MagicMock()
        with patch.object(dash, "_tel", MagicMock()), \
             patch.object(dash, "_slew_rejection", return_value=None), \
             patch.object(dash, "_run_centering_bg", centering), \
             patch.object(dash, "_run_stacking_bg", MagicMock()), \
             patch.object(dash, "_safety_mgr", None), \
             patch("threading.Thread", MagicMock()):
            dash._run_imaging_block(self.TARGET)
        centering.assert_called_once()
        self.assertAlmostEqual(centering.call_args[0][0], 5.588 * 15.0, places=2)

    def test_a_failed_centring_still_stacks(self):
        """An uncentred frame is still a frame; giving up wastes the night."""
        with dash._sched_lock:
            dash._sched_state["cancelled"] = True
        started = MagicMock()
        with patch.object(dash, "_tel", MagicMock()), \
             patch.object(dash, "_slew_rejection", return_value=None), \
             patch.object(dash, "_run_centering_bg",
                          MagicMock(side_effect=RuntimeError("no solve"))), \
             patch.object(dash, "_safety_mgr", None), \
             patch("threading.Thread", started):
            dash._run_imaging_block(self.TARGET)
        self.assertTrue(started.called, "stacking should still have started")

    def test_an_unreachable_target_is_not_slewed_to(self):
        tel = MagicMock()
        with patch.object(dash, "_tel", tel), \
             patch.object(dash, "_slew_rejection", return_value="below horizon"):
            dash._run_imaging_block(self.TARGET)
        tel.slew_to_coordinates.assert_not_called()

    def test_no_target_available_is_not_an_error(self):
        with patch.object(dash, "_pick_imaging_target", return_value=None):
            dash._run_imaging_block()           # must not raise
        self.assertFalse(dash.imaging_status()["running"])

    def test_the_status_is_cleared_when_the_block_ends(self):
        self._run()
        self.assertFalse(dash.imaging_status()["running"])

    def test_a_failed_slew_is_recorded_not_swallowed(self):
        tel = MagicMock()
        tel.slew_to_coordinates.side_effect = RuntimeError("mount stalled")
        with patch.object(dash, "_tel", tel), \
             patch.object(dash, "_slew_rejection", return_value=None), \
             patch.object(dash, "_safety_mgr", None):
            dash._run_imaging_block(self.TARGET)
        status = dash.imaging_status()
        self.assertFalse(status["running"])
        self.assertIn("mount stalled", status["error"] or "")


class HandoffConditionTest(unittest.TestCase):
    """When the handover should and should not happen."""

    def setUp(self):
        with dash._tonight_lock:
            dash._tonight.clear()
        self.addCleanup(lambda: dash._tonight.clear())

    def _intent(self, hours, imaging, observing=True):
        with dash._tonight_lock:
            dash._tonight.clear()
            dash._tonight.update({
                "observing": observing, "status": "accepted",
                "proposal": {"research_hours": hours, "imaging_after": imaging},
            })

    def test_an_unbounded_research_night_never_hands_over(self):
        self._intent(hours=4, imaging=False)
        with dash._sched_lock:
            dash._sched_state["started_at"] = 0.0     # long ago
        self.assertFalse(dash._research_window_expired())

    def test_a_bounded_night_hands_over_once_the_hours_are_up(self):
        import time
        self._intent(hours=2, imaging=True)
        with dash._sched_lock:
            dash._sched_state["started_at"] = time.time() - (2 * 3600 + 60)
        self.assertTrue(dash._research_window_expired())

    def test_a_stood_down_node_does_not_start_imaging(self):
        """The override has to stop the whole night, not just the research."""
        self._intent(hours=2, imaging=True, observing=False)
        self.assertFalse(dash._tonight_allows_observing())


if __name__ == "__main__":
    unittest.main()


class BrowseTargetsTest(unittest.TestCase):
    """What the browse tool offers must match what the handover would pick.

    They were separate: _pick_imaging_target filtered by type, while the browse
    tool returned the raw catalogue. Under fault testing it duly suggested
    B033 -- a dark nebula, which is by definition the absence of anything to
    photograph.
    """

    def setUp(self):
        self.client = dash.app.test_client()

    def test_only_imaging_worthy_types_are_offered(self):
        body = self.client.get("/api/imaging/targets?limit=50").get_json()
        self.assertTrue(body["targets"])
        for obj in body["targets"]:
            self.assertIn(obj["type"], dash._IMAGING_TYPES)

    def test_dark_nebulae_are_not_offered(self):
        body = self.client.get("/api/imaging/targets?limit=200").get_json()
        self.assertNotIn("Dark Nebula", {o["type"] for o in body["targets"]})

    def test_the_best_suggestions_come_first(self):
        body = self.client.get("/api/imaging/targets?limit=5").get_json()
        ids = [o["id"] for o in body["targets"]]
        self.assertTrue(all(i.startswith("M") and i[1:].isdigit() for i in ids), ids)

    def test_search_still_works(self):
        body = self.client.get("/api/imaging/targets?search=orion").get_json()
        self.assertTrue(body["targets"])
        self.assertTrue(any("orion" in str(o.get("name", "")).lower()
                            for o in body["targets"]))

    def test_reachable_only_uses_the_same_safety_gate_as_the_handover(self):
        with patch.object(dash, "_slew_rejection", return_value="below horizon"):
            body = self.client.get(
                "/api/imaging/targets?reachable=1&limit=5").get_json()
        self.assertEqual(body["targets"], [],
                         "nothing is reachable, so nothing should be offered")
        self.assertTrue(body["reachable_only"])

    def test_a_silly_limit_does_not_dump_the_whole_catalogue(self):
        body = self.client.get("/api/imaging/targets?limit=99999").get_json()
        self.assertLessEqual(len(body["targets"]), 200)

    def test_a_malformed_limit_falls_back_to_a_default(self):
        body = self.client.get("/api/imaging/targets?limit=soon").get_json()
        self.assertEqual(len(body["targets"]), 20)
