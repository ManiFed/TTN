#!/usr/bin/env python3
"""Focused tests for the timeline/explanation/activity feature wave."""

import unittest
from unittest.mock import patch

from cloud import incidents, scheduler, scoring


class ScoreExplanationTest(unittest.TestCase):
    def test_explain_score_ranks_weighted_factors(self):
        explanation = scoring.explain_score(
            {"target_type": "CV"},
            {"node_id": "node_a"},
            {
                "brightness": 0.4,
                "science": 1.0,
                "time": 0.8,
                "coverage": 0.2,
                "observe": 0.9,
                "best_alt_deg": 67.2,
                "visibility_minutes": 180,
                "reliability_score": 0.75,
            },
            {
                "brightness": 0.2,
                "science": 0.25,
                "time": 0.15,
                "coverage": 0.15,
                "observe": 0.25,
            },
        )

        self.assertIn("Strongest factors", explanation["summary"])
        self.assertEqual(explanation["factors"][0]["key"], "science")
        self.assertEqual(explanation["node_id"], "node_a")
        self.assertEqual(explanation["best_alt_deg"], 67.2)

    def test_score_includes_balanced_science_roi_component(self):
        target = {
            "target_id": "T",
            "name": "T CrB",
            "target_type": "nova",
            "ra_deg": 10.0,
            "dec_deg": 20.0,
            "mag": 10.0,
            "priority": 0.8,
            "time_critical": 1,
            "cadence_hours": 6.0,
            "discovered_at": "2026-06-25T00:00:00+00:00",
        }
        node = {
            "node_id": "node_a",
            "mag_bright_limit": 6.0,
            "mag_faint_limit": 15.5,
            "reliability_score": 0.8,
            "scheduler_trust_score": 0.8,
        }
        config = {
            "scoring": {
                "weights": {
                    "brightness": 0.0,
                    "science": 0.0,
                    "time": 0.0,
                    "coverage": 0.0,
                    "observe": 0.5,
                    "roi": 0.5,
                }
            }
        }

        with patch("cloud.scoring.observability", return_value=(1.0, 120.0, 60.0)), \
             patch("cloud.scoring.coverage_gap", return_value=0.4), \
             patch("cloud.scoring.science_roi", return_value=0.9):
            high = scoring.score_target_for_node(target, node, None, 1.0, config, {})

        with patch("cloud.scoring.observability", return_value=(1.0, 120.0, 60.0)), \
             patch("cloud.scoring.coverage_gap", return_value=0.4), \
             patch("cloud.scoring.science_roi", return_value=0.1):
            low = scoring.score_target_for_node(target, node, None, 1.0, config, {})

        self.assertEqual(high["roi"], 0.9)
        self.assertGreater(high["total"], low["total"])
        self.assertIn("roi_score", high["explanation"])


class IncidentClassificationTest(unittest.TestCase):
    def test_classifies_failures_by_scheduler_attribution(self):
        self.assertEqual(
            incidents.classify("plate_solve_failed")["attribution"],
            "node",
        )
        self.assertEqual(
            incidents.classify("clouds_blocked_observation")["attribution"],
            "environment",
        )
        self.assertEqual(
            incidents.classify("plan_generation_failed")["attribution"],
            "system",
        )

    def test_node_incident_penalty_is_stronger_than_environment_penalty(self):
        node_rows = [
            {"incident_type": "plate_solve_failed", "severity": "warning"},
            {"incident_type": "device_disconnect", "severity": "error"},
        ]
        env_rows = [
            {"incident_type": "clouds_blocked_observation", "severity": "warning"},
            {"incident_type": "poor_seeing", "severity": "warning"},
        ]

        with patch("cloud.incidents.db.query", return_value=node_rows):
            node_penalty = incidents.recent_scheduler_penalty("node_a")
        with patch("cloud.incidents.db.query", return_value=env_rows):
            env_penalty = incidents.recent_scheduler_penalty("node_a")

        self.assertGreater(node_penalty, env_penalty)
        self.assertLess(node_penalty, 1.0)


class LongitudeDiversityTest(unittest.TestCase):
    def test_reserved_nearby_wraps_across_dateline(self):
        reservations = {"T": [179.0]}

        self.assertTrue(scheduler._reserved_nearby(reservations, "T", -179.0, 5.0))
        self.assertFalse(scheduler._reserved_nearby(reservations, "T", -150.0, 5.0))


if __name__ == "__main__":
    unittest.main()
