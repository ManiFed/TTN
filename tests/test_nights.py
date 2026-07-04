#!/usr/bin/env python3
"""Tests for local-night summary generation and missed-night notifications."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from cloud import nights


class FakeNightDb:
    def __init__(self):
        self.nodes = []
        self.summaries = set()
        self.plans = set()
        self.members = []
        self.executed = []

    def query(self, sql, params=()):
        if "FROM nodes" in sql:
            return self.nodes
        if "FROM measurements" in sql:
            return []
        if "FROM node_members" in sql:
            return self.members
        return []

    def query_one(self, sql, params=()):
        if "FROM nodes" in sql:
            return {"utc_offset_hours": self.nodes[0].get("utc_offset_hours", 0)}
        if "FROM night_summaries" in sql:
            return {"id": 1} if tuple(params) in self.summaries else None
        if "FROM plans" in sql:
            return {"plan_id": "plan_1"} if tuple(params) in self.plans else None
        return None

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if "INSERT INTO night_summaries" in sql:
            self.summaries.add((params[0], params[1]))


class CompletedNightTest(unittest.TestCase):
    def test_uses_node_local_completed_night_not_utc_yesterday(self):
        now = datetime(2026, 7, 4, 3, 2, tzinfo=timezone.utc)  # Jul 3 23:02 EDT
        self.assertEqual(nights._completed_night_for_node(now, -4), "2026-07-02")

    def test_after_local_morning_summarizes_previous_evening(self):
        now = datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc)  # Jul 4 09:00 EDT
        self.assertEqual(nights._completed_night_for_node(now, -4), "2026-07-03")


class PendingSummariesTest(unittest.TestCase):
    def test_does_not_flag_tonight_while_local_night_is_running(self):
        fake = FakeNightDb()
        fake.nodes = [{"node_id": "node_1", "utc_offset_hours": -4, "status": "active"}]
        fake.plans.add(("node_1", "2026-07-03"))
        now = datetime(2026, 7, 4, 3, 2, tzinfo=timezone.utc)

        with patch.object(nights, "db", fake):
            generated = nights.generate_pending_summaries({}, now=now)

        self.assertEqual(generated, 0)
        self.assertFalse(any("notifications" in sql for sql, _ in fake.executed))

    def test_missed_planned_night_is_recorded_once(self):
        fake = FakeNightDb()
        fake.nodes = [{"node_id": "node_1", "utc_offset_hours": -4, "status": "active"}]
        fake.plans.add(("node_1", "2026-07-03"))
        fake.members = [{"user_id": "user_1", "push_token": "", "notification_push": 0}]
        now = datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc)

        with patch.object(nights, "db", fake):
            first = nights.generate_pending_summaries({}, now=now)
            second = nights.generate_pending_summaries({}, now=now)

        notification_writes = [
            params for sql, params in fake.executed if "INSERT INTO notifications" in sql
        ]
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(notification_writes), 1)
        self.assertIn(("node_1", "2026-07-03"), fake.summaries)


if __name__ == "__main__":
    unittest.main()
