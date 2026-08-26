#!/usr/bin/env python3
"""Tonight's intent: who decides, and who wins when they disagree.

The precedence rules are the whole design, and each one protects something:

  a stand-down beats everything      a member saying stop is an instruction
  weather beats an acceptance        an open telescope in rain is a broken one
  silence beats nothing, then yes    a telescope nobody answers for still works
  a decline lasts one night          opting out must not quietly become opting
                                     out forever

Getting any of these backwards is the kind of bug that either soaks someone's
telescope or silently removes it from the network, so they are pinned here.

Run with:  python3 -m pytest tests/test_nightly_intent.py
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cloud import nightly


class _FakeDb:
    """Enough of cloud.db to exercise night_intents, and nothing more."""

    def __init__(self):
        self.rows: list[dict] = []
        self.vacations: list[tuple] = []

    # -- helpers the module uses ------------------------------------------
    @staticmethod
    def loads(text, default=None):
        if text is None:
            return default
        if isinstance(text, (dict, list)):
            return text
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return default

    def _find(self, node_id, night):
        for row in self.rows:
            if row["node_id"] == node_id and row["night"] == night:
                return row
        return None

    def query_one(self, sql, params=()):
        if "night_intents" in sql:
            return self._find(params[0], params[1])
        raise AssertionError(f"unexpected query_one: {sql[:60]}")

    def execute(self, sql, params=(), returning_id=False):
        if "INSERT INTO night_intents" in sql:
            node_id, night, status, proposal, respond_by, created = params
            if self._find(node_id, night) is None:      # ON CONFLICT DO NOTHING
                self.rows.append({
                    "id": len(self.rows) + 1, "node_id": node_id, "night": night,
                    "status": status, "proposal_json": proposal,
                    "respond_by": respond_by, "decided_at": "",
                    "decided_via": "", "note": "", "created_at": created,
                })
            return 1
        if "UPDATE night_intents" in sql:
            # The auto-accept is guarded (… AND status = %s) so it cannot race
            # a member's answer; its parameters therefore end differently.
            if "AND status = %s" in sql:
                node_id, night, expected = params[-3], params[-2], params[-1]
                row = self._find(node_id, night)
                if row is None or row["status"] != expected:
                    return 0
                row["status"], row["decided_at"], row["decided_via"] = (
                    params[0], params[1], "auto")
                return 1
            node_id, night = params[-2], params[-1]
            row = self._find(node_id, night)
            if row is None:
                return 0
            fields = []
            if "status = %s" in sql:
                fields.append("status")
            if "proposal_json = %s" in sql:
                fields.append("proposal_json")
            if "decided_at = %s" in sql:
                fields.append("decided_at")
            if "decided_via = 'member'" in sql:
                row["decided_via"] = "member"
            if "decided_via = 'override'" in sql:
                row["decided_via"] = "override"
            if "decided_via = 'weather'" in sql:
                row["decided_via"] = "weather"
            if "note = %s" in sql:
                fields.append("note")
            for name, value in zip(fields, params):
                row[name] = value
            return 1
        raise AssertionError(f"unexpected execute: {sql[:60]}")


NODE = {"node_id": "node_1", "utc_offset_hours": 0.0,
        "latitude": 51.5, "longitude": -0.1}

def _forecast(cloud_pct: float, precip: str = "none") -> dict:
    """A minimal real-shaped 7timer ASTRO forecast: one hourly slot, now.

    fetch_astronomy_weather returns hourly series keyed by "times", not a
    single value -- this mirrors that shape so the mock matches what the
    real function actually hands weather_verdict.
    """
    now = datetime.now(timezone.utc)
    return {"times": [now], "cloud_cover": [cloud_pct], "precip_type": [precip]}


CLEAR = _forecast(10)
RAIN = _forecast(90, precip="rain")
OVERCAST = _forecast(95)


class _Base(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDb()
        self._db_patch = patch.object(nightly, "db", self.db)
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)

    def resolve(self, forecast=CLEAR, node=None):
        """Resolve with a given forecast, exercising the real weather logic.

        Patches the forecast *source* rather than weather_verdict itself, so
        the thresholds under test are the ones that actually ship.
        """
        with patch("cloud.conditions.fetch_astronomy_weather",
                   return_value=forecast):
            return nightly.resolve(node or NODE)

    def past_deadline(self):
        for row in self.db.rows:
            row["respond_by"] = (datetime.now(timezone.utc)
                                 - timedelta(hours=1)).isoformat()

    def future_deadline(self):
        for row in self.db.rows:
            row["respond_by"] = (datetime.now(timezone.utc)
                                 + timedelta(hours=6)).isoformat()


class ProposalTest(_Base):

    def test_a_proposal_is_created_on_first_look(self):
        intent = nightly.get_or_create(NODE)
        self.assertEqual(intent["status"], nightly.PROPOSED)
        self.assertEqual(intent["proposal"]["mode"], "research")

    def test_the_default_recommendation_is_research(self):
        proposal = nightly.build_proposal(NODE)
        self.assertEqual(proposal["mode"], "research")
        self.assertGreater(proposal["research_hours"], 0)
        self.assertTrue(proposal["imaging_after"],
                        "imaging should be offered after research, not instead")

    def test_looking_twice_does_not_create_two_nights(self):
        nightly.get_or_create(NODE)
        nightly.get_or_create(NODE)
        self.assertEqual(len(self.db.rows), 1)


class SilenceBecomesConsentTest(_Base):

    def test_before_the_deadline_nothing_has_been_decided(self):
        nightly.get_or_create(NODE)
        self.future_deadline()
        verdict = self.resolve()
        self.assertFalse(verdict["observing"])
        self.assertEqual(verdict["status"], nightly.PROPOSED)

    def test_after_the_deadline_the_recommendation_runs(self):
        """A telescope nobody answered for still contributes."""
        nightly.get_or_create(NODE)
        self.past_deadline()
        verdict = self.resolve()
        self.assertTrue(verdict["observing"])
        self.assertEqual(verdict["status"], nightly.AUTO)
        self.assertEqual(verdict["decided_via"], "auto")


class MemberDecisionTest(_Base):

    def test_accepting_observes_immediately_without_waiting_for_dusk(self):
        nightly.respond(NODE, "accept")
        self.future_deadline()
        verdict = self.resolve()
        self.assertTrue(verdict["observing"])
        self.assertEqual(verdict["status"], nightly.ACCEPTED)

    def test_accepting_can_reshape_the_run(self):
        intent = nightly.respond(NODE, "accept", research_hours=2,
                                 imaging_after=False)
        self.assertEqual(intent["proposal"]["research_hours"], 2.0)
        self.assertFalse(intent["proposal"]["imaging_after"])

    def test_declining_stops_tonight(self):
        nightly.respond(NODE, "decline")
        self.past_deadline()
        verdict = self.resolve()
        self.assertFalse(verdict["observing"])
        self.assertEqual(verdict["status"], nightly.DECLINED)

    def test_a_decline_does_not_leak_into_tomorrow(self):
        """Opting out of one night must not quietly opt out of every night."""
        nightly.respond(NODE, "decline")
        tomorrow = {**NODE, "utc_offset_hours": 0.0}
        with patch.object(nightly, "tonight_date", return_value="2099-01-01"):
            fresh = nightly.get_or_create(tomorrow)
        self.assertEqual(fresh["status"], nightly.PROPOSED)

    def test_an_invalid_decision_is_rejected(self):
        with self.assertRaises(ValueError):
            nightly.respond(NODE, "maybe")


class WeatherTest(_Base):

    def test_rain_holds_the_night(self):
        nightly.respond(NODE, "accept")
        verdict = self.resolve(forecast=RAIN)
        self.assertFalse(verdict["observing"])
        self.assertEqual(verdict["status"], nightly.WEATHER_HOLD)

    def test_overcast_holds_the_night(self):
        nightly.respond(NODE, "accept")
        verdict = self.resolve(forecast=OVERCAST)
        self.assertFalse(verdict["observing"])

    def test_weather_overrules_an_acceptance(self):
        """The member said yes; the sky said no. The sky wins."""
        nightly.respond(NODE, "accept")
        self.assertFalse(self.resolve(forecast=RAIN)["observing"])

    def test_weather_overrules_the_auto_path_too(self):
        nightly.get_or_create(NODE)
        self.past_deadline()
        self.assertFalse(self.resolve(forecast=RAIN)["observing"])

    def test_a_clearing_forecast_restores_the_night(self):
        """A hold is not a cancellation — four hours is a long time."""
        nightly.respond(NODE, "accept")
        self.assertFalse(self.resolve(forecast=RAIN)["observing"])
        verdict = self.resolve(forecast=CLEAR)
        self.assertTrue(verdict["observing"],
                        "the night should resume once the forecast improves")

    def test_a_missing_forecast_does_not_stop_observing(self):
        """Failing closed here would silently idle the fleet on an API outage."""
        with patch("cloud.conditions.fetch_astronomy_weather", return_value=None):
            verdict = nightly.weather_verdict(NODE)
        self.assertTrue(verdict["observable"])
        self.assertEqual(verdict["source"], "none")

    def test_a_weather_lookup_that_raises_does_not_stop_observing(self):
        with patch("cloud.conditions.fetch_astronomy_weather",
                   side_effect=RuntimeError("upstream down")):
            self.assertTrue(nightly.weather_verdict(NODE)["observable"])


class OverrideTest(_Base):

    def test_standing_down_stops_the_night(self):
        nightly.respond(NODE, "accept")
        nightly.stand_down(NODE, reason="mount is making a noise")
        verdict = self.resolve()
        self.assertFalse(verdict["observing"])
        self.assertEqual(verdict["status"], nightly.STOOD_DOWN)

    def test_a_stand_down_beats_the_auto_path(self):
        """The deadline must never resurrect a night a member stopped."""
        nightly.stand_down(NODE, reason="away")
        self.past_deadline()
        self.assertFalse(self.resolve()["observing"])

    def test_a_stand_down_survives_the_weather_clearing(self):
        """Good weather is not permission — only the member is."""
        nightly.stand_down(NODE, reason="lending the scope out")
        self.assertFalse(self.resolve(forecast=CLEAR)["observing"])

    def test_the_reason_is_carried_back_to_the_member(self):
        nightly.stand_down(NODE, reason="dew on the corrector")
        self.assertIn("dew on the corrector", self.resolve()["reason"])

    def test_standing_down_for_several_nights_parks_the_node(self):
        """Multi-night opt-out reuses vacation rather than inventing a rival."""
        with patch.object(nightly.registry, "set_vacation") as set_vacation:
            nightly.stand_down(NODE, reason="holiday", nights=7)
        set_vacation.assert_called_once()
        node_id, until = set_vacation.call_args[0]
        self.assertEqual(node_id, "node_1")
        self.assertGreater(until, datetime.now(timezone.utc).date().isoformat())

    def test_a_single_night_stand_down_does_not_park_the_node(self):
        with patch.object(nightly.registry, "set_vacation") as set_vacation:
            nightly.stand_down(NODE, reason="just tonight")
        set_vacation.assert_not_called()


class PlannerGateTest(_Base):

    def test_observing_tonight_follows_the_verdict(self):
        nightly.stand_down(NODE)
        with patch.object(nightly, "weather_verdict",
                          return_value={"observable": True, "reason": ""}):
            self.assertFalse(nightly.observing_tonight(NODE))

    def test_a_failure_fails_open_rather_than_idling_a_telescope(self):
        """A bug in this module must not silently remove a working telescope."""
        with patch.object(nightly, "resolve", side_effect=RuntimeError("boom")):
            self.assertTrue(nightly.observing_tonight(NODE))


class NightNamingTest(unittest.TestCase):
    """A night is named for the evening it starts, in the node's local time."""

    def test_evening_and_the_small_hours_are_the_same_night(self):
        evening = datetime(2026, 8, 21, 22, 0, tzinfo=timezone.utc)
        small_hours = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
        with patch.object(nightly, "_now", return_value=evening):
            first = nightly.tonight_date(NODE)
        with patch.object(nightly, "_now", return_value=small_hours):
            second = nightly.tonight_date(NODE)
        self.assertEqual(first, second)
        self.assertEqual(first, "2026-08-21")

    def test_local_offset_is_respected(self):
        """A node in Hawaii is still on yesterday evening when UTC has rolled."""
        utc = datetime(2026, 8, 22, 6, 0, tzinfo=timezone.utc)
        hawaii = {**NODE, "utc_offset_hours": -10.0}
        with patch.object(nightly, "_now", return_value=utc):
            self.assertEqual(nightly.tonight_date(hawaii), "2026-08-21")


if __name__ == "__main__":
    unittest.main()
