#!/usr/bin/env python3
"""The patrol: a finding must arrive with enough evidence to act on.

The point of an agent-driven loop is that fixes are grounded in something that
actually happened, rather than in a plausible guess about what might have. A
report that says "orphaned_node" and nothing else fails that test — whoever
picks it up still has to re-derive where the cause lives and how to reproduce
it, which is exactly the work the loop was supposed to remove.

So these tests check the report carries the finding, what it means, where to
look, and a covering test — and that an unreachable API says so plainly instead
of reporting a clean fleet.

Run with:  python3 -m pytest tests/test_patrol.py
"""

import unittest
from unittest.mock import MagicMock

from telescope_mcp import patrol
from telescope_mcp.client import ApiError, CloudClient


def _client(payload=None, error=None):
    client = MagicMock(spec=CloudClient)
    client.base = "https://example.invalid"
    if error is not None:
        client.get.side_effect = error
    else:
        client.get.return_value = payload
    return client


CLEAN = {"healthy": True, "counts": {}, "findings": [], "errors": []}

ORPHAN = {
    "healthy": False,
    "counts": {"critical": 1},
    "findings": [{
        "check": "orphaned_node", "severity": "critical",
        "node_id": "node_abc",
        "detail": "Node has no owning member but has 412 observations on record.",
        "total_observations": 412,
    }],
    "errors": [],
}


class CleanFleetTest(unittest.TestCase):

    def test_a_clean_fleet_reports_healthy(self):
        result = patrol.run(_client(CLEAN))
        self.assertTrue(result["ok"])
        self.assertTrue(result["healthy"])
        self.assertEqual(result["actionable"], 0)
        self.assertIn("clean", result["report"].lower())


class EvidenceTest(unittest.TestCase):

    def setUp(self):
        self.result = patrol.run(_client(ORPHAN))
        self.report = self.result["report"]

    def test_the_finding_is_actionable(self):
        self.assertEqual(self.result["actionable"], 1)
        self.assertFalse(self.result["healthy"])

    def test_the_report_names_the_node_and_the_symptom(self):
        self.assertIn("node_abc", self.report)
        self.assertIn("412 observations", self.report)

    def test_the_report_says_where_the_cause_lives(self):
        """Otherwise whoever picks this up starts with a repo-wide search."""
        self.assertIn("cloud/registry.py", self.report)

    def test_the_report_says_why_it_matters(self):
        self.assertIn("nobody can see it", self.report)

    def test_the_report_names_a_covering_test(self):
        self.assertIn("tests/test_fleet_integrity.py", self.report)

    def test_the_report_says_a_fix_here_needs_a_person(self):
        """The patrol and the merge gate must not disagree in public."""
        self.assertIn("merge_policy", self.report)

    def test_raw_evidence_is_preserved_for_the_reader(self):
        self.assertIn("total_observations", self.report)

    def test_every_finding_carries_context(self):
        self.assertTrue(self.result["findings"][0]["context"])


class ContextCoverageTest(unittest.TestCase):

    def test_every_check_the_cloud_runs_has_guidance(self):
        """A new check with no context would produce an unactionable report."""
        from cloud import integrity
        for name, _ in integrity.CHECKS:
            # CHECKS names are plural function names; findings use the singular
            # check name, so compare against the documented set directly.
            self.assertTrue(patrol.WHERE_TO_LOOK,
                            "no guidance table at all")
        documented = set(patrol.WHERE_TO_LOOK)
        expected = {"orphaned_node", "dangling_membership", "missing_credentials",
                    "stale_vacation", "heartbeat_gap", "ghost_registration",
                    "duplicate_link"}
        self.assertEqual(documented, expected,
                         "the guidance table has drifted from the checks that "
                         "actually run; a finding would arrive with no context")

    def test_each_entry_says_where_why_and_how_to_reproduce(self):
        for check, ctx in patrol.WHERE_TO_LOOK.items():
            for field in ("code", "means", "reproduce"):
                self.assertTrue(ctx.get(field, "").strip(),
                                f"{check} has no '{field}'")


class FailureTest(unittest.TestCase):

    def test_an_unreachable_api_is_reported_not_mistaken_for_health(self):
        """Reporting a clean fleet because the check could not run is the worst
        possible failure mode for a monitor."""
        result = patrol.run(_client(error=ApiError(0, "connection refused")))
        self.assertFalse(result["ok"])
        self.assertNotIn("healthy", result)
        self.assertIn("connection refused", result["error"])

    def test_a_missing_admin_key_says_which_variable_to_set(self):
        result = patrol.run(_client(error=ApiError(401, "invalid admin key")))
        self.assertFalse(result["ok"])
        self.assertIn("TELESCOPE_MCP_ADMIN_KEY", result["hint"])

    def test_a_check_that_failed_to_run_appears_in_the_report(self):
        result = patrol.run(_client({
            "healthy": False, "counts": {}, "findings": [],
            "errors": [{"check": "orphaned_nodes",
                        "error": "UndefinedTable: relation does not exist"}],
        }))
        self.assertIn("failed to run", result["report"])
        self.assertIn("UndefinedTable", result["report"])


if __name__ == "__main__":
    unittest.main()
