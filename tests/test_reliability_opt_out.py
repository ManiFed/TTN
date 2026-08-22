#!/usr/bin/env python3
"""Choosing not to observe must not look like failing to observe.

The observing-rate term in refresh_node_performance counted nights with no
measurements against a node, whatever the reason. A member on holiday scored
exactly like a member whose mount had seized -- and that number multiplies tile
value in event_tiling, so a week away cost them their share of transient
response for the following month.

The fix removes opted-out nights from the denominator. The tests that matter
are the ones proving it did not go too far: a telescope that was available and
silent must still be penalised, or the term stops meaning anything.

Run with:  python3 -m pytest tests/test_reliability_opt_out.py
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cloud import registry


def _nights_ago(n: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


class _FakeDb:
    def __init__(self, intents=None, node_row=None):
        self.intents = intents or []
        self.node_row = node_row

    def query(self, sql, params=()):
        if "night_intents" in sql:
            return self.intents
        return []

    def query_one(self, sql, params=()):
        if "FROM nodes" in sql:
            return self.node_row
        return None


class OptedOutNightsTest(unittest.TestCase):

    def _run(self, intents=None, node_row=None):
        with patch.object(registry, "db", _FakeDb(intents, node_row)):
            return registry.opted_out_nights("node_1", node_row)

    def test_declined_and_stood_down_nights_are_counted(self):
        nights = self._run(intents=[
            {"night": _nights_ago(1), "status": "declined"},
            {"night": _nights_ago(2), "status": "stood_down"},
        ])
        self.assertEqual(len(nights), 2)

    def test_accepted_and_auto_nights_are_not(self):
        nights = self._run(intents=[
            {"night": _nights_ago(1), "status": "accepted"},
            {"night": _nights_ago(2), "status": "auto"},
            {"night": _nights_ago(3), "status": "proposed"},
        ])
        self.assertEqual(nights, set())

    def test_a_weather_hold_still_counts_against_the_node(self):
        """Deliberate: a site that is clouded out often genuinely delivers
        less, and that is legitimate scheduling information. Only member
        choice is excused."""
        nights = self._run(intents=[
            {"night": _nights_ago(1), "status": "weather_hold"},
        ])
        self.assertEqual(nights, set())

    def test_a_vacation_window_is_counted(self):
        nights = self._run(node_row={
            "vacation_from": _nights_ago(6), "vacation_until": _nights_ago(2)})
        self.assertEqual(len(nights), 5, "five nights inclusive")

    def test_a_vacation_running_into_the_future_stops_at_today(self):
        future = (datetime.now(timezone.utc).date() + timedelta(days=10)).isoformat()
        nights = self._run(node_row={
            "vacation_from": _nights_ago(2), "vacation_until": future})
        self.assertLessEqual(len(nights), 3,
                             "nights that have not happened cannot be absences")

    def test_a_vacation_before_the_window_is_ignored(self):
        nights = self._run(node_row={
            "vacation_from": _nights_ago(90), "vacation_until": _nights_ago(60)})
        self.assertEqual(nights, set())

    def test_overlapping_vacation_and_decline_are_not_double_counted(self):
        night = _nights_ago(3)
        nights = self._run(
            intents=[{"night": night, "status": "declined"}],
            node_row={"vacation_from": night, "vacation_until": night})
        self.assertEqual(len(nights), 1)

    def test_a_malformed_vacation_date_does_not_crash(self):
        self.assertEqual(self._run(node_row={"vacation_until": "soon"}), set())

    def test_a_missing_intents_table_fails_open(self):
        """An un-migrated database must score nodes the way it always did,
        not silently exempt every one of them."""
        class Exploding:
            def query(self, sql, params=()):
                raise RuntimeError('relation "night_intents" does not exist')
            def query_one(self, sql, params=()):
                return None
        with patch.object(registry, "db", Exploding()):
            self.assertEqual(registry.opted_out_nights("node_1", None), set())


class ObservingRateTest(unittest.TestCase):
    """The term itself: what the 0.20 weight now rewards."""

    def _rate(self, clear_nights: int, opted_out: int) -> float:
        available = max(0, 30 - opted_out)
        if available < registry._MIN_AVAILABLE_NIGHTS:
            return 1.0
        return min(1.0, clear_nights / available)

    def test_a_holiday_no_longer_reads_as_failure(self):
        """Observed every night they were available, away for seven."""
        self.assertEqual(self._rate(clear_nights=23, opted_out=7), 1.0)

    def test_a_broken_telescope_is_still_penalised(self):
        """Available all thirty nights, delivered on twenty-three. The term has
        to keep meaning something."""
        self.assertAlmostEqual(self._rate(clear_nights=23, opted_out=0),
                               23 / 30, places=4)

    def test_being_away_the_whole_window_is_not_held_against_anyone(self):
        self.assertEqual(self._rate(clear_nights=0, opted_out=30), 1.0)

    def test_a_node_available_and_entirely_silent_scores_zero(self):
        self.assertEqual(self._rate(clear_nights=0, opted_out=0), 0.0)

    def test_the_rate_is_capped_at_one(self):
        self.assertEqual(self._rate(clear_nights=40, opted_out=0), 1.0)

    def test_the_old_behaviour_is_unchanged_when_nobody_opts_out(self):
        """No opt-outs means the denominator is still thirty, so every existing
        node's score is exactly what it was."""
        for nights in (0, 7, 15, 23, 30):
            self.assertAlmostEqual(self._rate(nights, 0),
                                   min(1.0, nights / 30.0), places=6)


class ImpactTest(unittest.TestCase):
    """What this was actually costing members."""

    def test_a_week_away_used_to_cost_the_same_as_a_week_broken(self):
        before_away = 0.20 * min(1.0, 23 / 30.0)
        before_broken = 0.20 * min(1.0, 23 / 30.0)
        self.assertEqual(before_away, before_broken,
                         "the old formula could not tell them apart")

        after_away = 0.20 * 1.0                    # 23 of 23 available nights
        after_broken = 0.20 * (23 / 30.0)          # 23 of 30 available nights
        self.assertGreater(after_away, after_broken,
                           "the new formula must tell them apart")
        self.assertAlmostEqual(after_away - after_broken, 0.0467, places=3)


if __name__ == "__main__":
    unittest.main()


class WiredInTest(unittest.TestCase):
    """Exercises refresh_node_performance itself.

    The tests above compute the rate independently, which proves the intended
    arithmetic but not that the function uses it. This drives the real code
    path so a fix that was never wired in would fail here.
    """

    class _Db:
        def __init__(self, clear_nights, intents, node_row):
            self.clear_nights = clear_nights
            self.intents = intents
            self.node_row = node_row
            self.updated = {}

        def query_one(self, sql, params=()):
            if "COUNT(DISTINCT date(received_at))" in sql:
                return {"n": self.clear_nights}
            if "validation_status = 'outlier'" in sql and "COUNT(*)" in sql:
                return {"n": 0}
            if "FROM measurements" in sql:
                # Plenty of history, all accepted, tight photometry, so the
                # observing-rate term is the only thing that can move.
                return {"total": 500, "accepted": 500, "outliers": 0,
                        "mean_unc": 0.02, "mean_fwhm": 3.0}
            if "FROM nodes" in sql:
                return self.node_row
            return None

        def query(self, sql, params=()):
            return self.intents if "night_intents" in sql else []

        def execute(self, sql, params=(), returning_id=False):
            if "UPDATE nodes SET" in sql:
                self.updated["params"] = params
            return 1

    def _reliability(self, clear_nights, intents=None, node_row=None):
        db = self._Db(clear_nights, intents or [], node_row)
        with patch.object(registry, "db", db), \
             patch.object(registry.incidents, "recent_scheduler_penalty",
                          return_value=0.0), \
             patch.object(registry.incidents, "auto_triage", return_value=None):
            return registry.refresh_node_performance("node_1")["reliability_score"]

    def test_away_scores_higher_than_broken_for_the_same_delivery(self):
        away = self._reliability(
            clear_nights=23,
            intents=[{"night": _nights_ago(i), "status": "declined"}
                     for i in range(1, 8)])
        broken = self._reliability(clear_nights=23)
        self.assertGreater(away, broken)
        self.assertAlmostEqual(away - broken, 0.0467, places=3)

    def test_a_vacationing_node_is_not_penalised(self):
        away = self._reliability(
            clear_nights=20,
            node_row={"vacation_from": _nights_ago(10),
                      "vacation_until": _nights_ago(1)})
        self.assertAlmostEqual(away, self._reliability(clear_nights=30), places=4)

    def test_a_node_that_never_opts_out_scores_exactly_as_before(self):
        """No regression for the entire existing fleet."""
        for nights in (0, 15, 30):
            expected = (0.40 * 1.0 + 0.25 * 1.0
                        + 0.20 * min(1.0, nights / 30.0)
                        + 0.15 * max(0.0, 1.0 - 0.02 / 0.30))
            self.assertAlmostEqual(self._reliability(clear_nights=nights),
                                   round(expected, 4), places=4)
