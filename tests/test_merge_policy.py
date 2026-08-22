#!/usr/bin/env python3
"""The gate that decides what an agent may merge without a person reading it.

If this file is wrong in the permissive direction, an unattended loop can land
a change to mount control, photometry maths or AAVSO output. The first points
somebody's telescope somewhere it should not go; the second two corrupt a
scientific record published under one obscode for the whole network, and are
invisible until someone downstream notices.

So the tests below are mostly of the form "this specific path must never be
auto-mergeable", named after what goes wrong if it is.

Run with:  python3 -m pytest tests/test_merge_policy.py
"""

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "merge_policy", REPO / "scripts" / "merge_policy.py")
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)


class BlastRadiusTest(unittest.TestCase):
    """Each of these, merged wrong and unattended, breaks something physical
    or scientific rather than merely a screen."""

    def assert_blocked(self, path: str):
        result = policy.classify([path])
        self.assertEqual(result["verdict"], policy.HUMAN,
                         f"{path} must not be auto-mergeable")
        self.assertTrue(result["blocking"][0]["reasons"],
                        f"{path} is blocked but the report says nothing about why")

    def test_mount_control_needs_a_person(self):
        for path in ("alpaca/telescope.py", "alpaca/camera.py",
                     "alpaca/covercalibrator.py", "alpaca/focuser.py",
                     "alpaca/filterwheel.py", "alpaca/autofocus.py"):
            self.assert_blocked(path)

    def test_the_safety_manager_needs_a_person(self):
        """The last thing between a mount and the sun."""
        self.assert_blocked("alpaca/safety_manager.py")

    def test_photometry_and_timing_need_a_person(self):
        for path in ("src/photometry.py", "src/timescales.py",
                     "src/plate_solve.py", "alpaca/platesolve.py",
                     "src/stacking.py", "src/calibration_identity.py",
                     "cloud/calibration.py", "cloud/transit_windows.py"):
            self.assert_blocked(path)

    def test_anything_published_to_aavso_needs_a_person(self):
        for path in ("src/aavso_submission.py", "cloud/data_pipeline.py"):
            self.assert_blocked(path)

    def test_identity_and_credentials_need_a_person(self):
        """The orphaning class of bug lives in exactly these files."""
        for path in ("cloud/registry.py", "cloud/auth.py",
                     "src/cloud_communicator.py"):
            self.assert_blocked(path)

    def test_schema_needs_a_person(self):
        self.assert_blocked("cloud/db.py")

    def test_the_gate_cannot_rewrite_itself(self):
        """A gate the loop can edit unattended is not a gate."""
        self.assert_blocked("scripts/merge_policy.py")
        self.assert_blocked(".github/workflows/merge-policy.yml")
        self.assert_blocked(".github/workflows/ci.yml")


class AutoMergeableTest(unittest.TestCase):
    """The gate has to actually let things through, or it will be turned off."""

    def assert_auto(self, *paths: str):
        result = policy.classify(list(paths))
        self.assertEqual(result["verdict"], policy.AUTO,
                         f"{paths} should be auto-mergeable but was blocked: "
                         f"{result['summary']}")

    def test_docs_and_app_screens_are_auto_mergeable(self):
        self.assert_auto("README.md", "docs/architecture.md",
                         "app/lib/screens/me_screen.dart")

    def test_tests_are_auto_mergeable(self):
        self.assert_auto("tests/test_nights.py", "tests/fuzz/faults.py")

    def test_most_of_the_mcp_surface_is_auto_mergeable(self):
        self.assert_auto("telescope_mcp/tools/network.py",
                         "telescope_mcp/tools/member.py",
                         "telescope_mcp/README.md")


class MixedChangeTest(unittest.TestCase):

    def test_one_protected_file_blocks_the_whole_change(self):
        """Splitting a change is the author's job, not the gate's."""
        result = policy.classify([
            "README.md", "app/lib/main.dart", "src/photometry.py"])
        self.assertEqual(result["verdict"], policy.HUMAN)
        self.assertEqual([b["path"] for b in result["blocking"]],
                         ["src/photometry.py"])
        self.assertIn("README.md", result["clear"])

    def test_the_summary_names_the_file_and_the_reason(self):
        result = policy.classify(["src/timescales.py"])
        self.assertIn("src/timescales.py", result["summary"])
        self.assertIn("BJD", result["summary"])

    def test_an_empty_change_is_not_treated_as_safe(self):
        """Otherwise a broken diff computation reads as 'nothing to review'."""
        result = policy.classify([])
        self.assertEqual(result["verdict"], policy.HUMAN)

    def test_every_protected_entry_has_a_reason(self):
        for glob, why in policy.PROTECTED:
            self.assertTrue(why.strip(),
                            f"{glob} is protected with no stated reason")


class CliTest(unittest.TestCase):

    def _run(self, *args) -> int:
        import contextlib, io, sys
        argv = sys.argv
        sys.argv = ["merge_policy.py", *args]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return policy.main()
        finally:
            sys.argv = argv

    def test_require_auto_passes_a_clear_change(self):
        self.assertEqual(self._run("--files", "README.md", "--require-auto"), 0)

    def test_require_auto_fails_a_protected_change(self):
        """This exit code is what blocks the merge in CI."""
        self.assertEqual(
            self._run("--files", "src/photometry.py", "--require-auto"), 1)

    def test_without_require_auto_it_only_reports(self):
        """A PR a human is already reading must not be failed by the gate."""
        self.assertEqual(self._run("--files", "src/photometry.py"), 0)


if __name__ == "__main__":
    unittest.main()
