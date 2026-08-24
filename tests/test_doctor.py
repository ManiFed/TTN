#!/usr/bin/env python3
"""The check that says which link is broken.

Three faults in a row looked identical from inside Claude -- the assistant
answered from general knowledge about telescopes. The member could not tell
them apart and neither could the person who built it, because when the tools
are missing nothing we wrote is running to say so. So this lives outside the
assistant and walks the chain in order.

Run with:  python3 -m pytest tests/test_doctor.py
"""

import json
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from telescope_mcp import doctor


class ElapsedTest(unittest.TestCase):
    """ps `etime` is [[DD-]HH:]MM:SS.

    The first version asked for `etimes`, which macOS ps does not support --
    and rather than erroring it silently drops the column, so the check
    answered "cannot tell whether Claude is running" every single time and hid
    the actual fault.
    """

    def test_minutes_and_seconds(self):
        self.assertEqual(doctor._elapsed_seconds("03:49"), 229)

    def test_hours(self):
        self.assertEqual(doctor._elapsed_seconds("01:02:03"), 3723)

    def test_days(self):
        self.assertEqual(doctor._elapsed_seconds("2-01:00:00"), 176400)


class ChainTest(unittest.TestCase):
    """Each link only matters if the one before it holds."""

    def _run(self, agent=True, registered=True, starts=True, restarted=True):
        def fake(name, ok, detail="", fix=""):
            return doctor._result(name, ok, detail or name, fix or "do a thing")
        # Every check takes the same argument, which is what lets run()
        # dispatch without special-casing any of them.
        with patch.object(doctor, "_agent_running",
                          side_effect=lambda base="": fake("node software", agent)), \
             patch.object(doctor, "_registered",
                          side_effect=lambda base="": fake("registered", registered)), \
             patch.object(doctor, "_command_starts",
                          side_effect=lambda base="": fake("answers", starts)), \
             patch.object(doctor, "_claude_restarted",
                          side_effect=lambda base="": fake("restarted", restarted)):
            return doctor.run("http://127.0.0.1:1")

    def test_a_healthy_chain_says_what_to_do_next(self):
        report = self._run()
        self.assertTrue(report["healthy"])
        self.assertIn("connect my telescope", report["summary"])

    def test_it_stops_at_the_first_broken_link(self):
        """Four complaints teach less than one, and later links cannot be
        judged once an earlier one is broken."""
        report = self._run(agent=False)
        self.assertEqual(len(report["checks"]), 1)
        self.assertFalse(report["healthy"])

    def test_a_later_failure_still_reports_the_earlier_successes(self):
        report = self._run(restarted=False)
        self.assertEqual(len(report["checks"]), 4)
        self.assertFalse(report["healthy"])

    def test_the_summary_is_the_broken_link_and_its_fix(self):
        report = self._run(registered=False)
        self.assertIn("registered", report["summary"])
        self.assertIn("do a thing", report["summary"])


class RegistrationCheckTest(unittest.TestCase):

    def _with_config(self, contents):
        import tempfile
        d = tempfile.mkdtemp()
        p = Path(d) / "claude_desktop_config.json"
        if contents is not None:
            p.write_text(contents)
        return p

    def test_a_missing_entry_explains_that_claude_drops_it(self):
        p = self._with_config('{"preferences": {}}')
        with patch.object(doctor.register_client, "config_path", return_value=p):
            r = doctor._registered()
        self.assertEqual(r["status"], doctor.BAD)
        self.assertIn("puts it back", r["fix"])

    def test_an_unparseable_config_is_reported_not_rewritten(self):
        p = self._with_config('{"mcpServers": ')
        with patch.object(doctor.register_client, "config_path", return_value=p):
            r = doctor._registered()
        self.assertEqual(r["status"], doctor.BAD)
        self.assertEqual(p.read_text(), '{"mcpServers": ')

    def test_an_opted_out_member_is_told_how_to_opt_back_in(self):
        p = self._with_config('{}')
        (p.parent / doctor.register_client.OPT_OUT_MARKER).write_text("")
        with patch.object(doctor.register_client, "config_path", return_value=p):
            r = doctor._registered()
        self.assertEqual(r["status"], doctor.BAD)
        self.assertIn("register itself again", r["fix"])

    def test_a_present_entry_passes(self):
        p = self._with_config(json.dumps(
            {"mcpServers": {"telescope-net": {"command": "x"}}}))
        with patch.object(doctor.register_client, "config_path", return_value=p):
            self.assertEqual(doctor._registered()["status"], doctor.OK)


class RestartCheckTest(unittest.TestCase):
    """The invisible one: Claude reads its tool list only at startup, so an
    entry written while it is open does nothing and says nothing."""

    def _check(self, claude_age_s, config_age_s):
        import tempfile, os
        p = Path(tempfile.mkdtemp()) / "claude_desktop_config.json"
        p.write_text("{}")
        os.utime(p, (time.time() - config_age_s,) * 2)
        line = f"  {int(claude_age_s // 60):02d}:{int(claude_age_s % 60):02d} /Applications/Claude.app/Contents/MacOS/Claude"
        with patch.object(doctor.register_client, "config_path", return_value=p), \
             patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run", return_value=MagicMock(stdout=line)):
            return doctor._claude_restarted()

    def test_claude_older_than_the_registration_fails(self):
        r = self._check(claude_age_s=600, config_age_s=60)
        self.assertEqual(r["status"], doctor.BAD)
        self.assertIn("Cmd-Q", r["fix"])

    def test_claude_started_after_the_registration_passes(self):
        self.assertEqual(self._check(claude_age_s=60, config_age_s=600)["status"],
                         doctor.OK)


if __name__ == "__main__":
    unittest.main()
