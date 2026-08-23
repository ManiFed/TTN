#!/usr/bin/env python3
"""What the installer tells a member, and what it opens when it finishes.

This drifted once already: the product moved to being driven by conversation
while the installer went on describing the desktop app as "your control
surface" and opening it on completion. A member installed it and was handed the
retired interface.

Copy is easy to leave behind because nothing fails when it is wrong, so the
claims that would embarrass us are pinned here.

Run with:  python3 -m pytest tests/test_installer_messaging.py
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WELCOME = (REPO / "build/macos/resources/welcome.html").read_text()
POSTINSTALL = (REPO / "build/macos/postinstall.sh").read_text()


class WelcomeTest(unittest.TestCase):

    def test_it_does_not_call_the_desktop_app_the_control_surface(self):
        """The sentence that sent a member to the retired app."""
        self.assertNotIn("The app is your control surface", WELCOME)
        self.assertNotIn("Telescope Net desktop app", WELCOME)

    def test_it_says_you_run_it_by_asking(self):
        self.assertIn("asking", WELCOME.lower())
        self.assertIn("connect my telescope", WELCOME)

    def test_station_mode_is_a_stated_requirement(self):
        """The single most common setup failure, and the telescope's own app
        reports "connected" in either mode."""
        self.assertIn("Station Mode", WELCOME)

    def test_it_names_the_assistants_that_work(self):
        for name in ("Claude Desktop", "Cursor", "Windsurf"):
            self.assertIn(name, WELCOME)

    def test_it_does_not_promise_chatgpt(self):
        """ChatGPT reaches MCP servers only as remote connectors, so it cannot
        start the node software or see a telescope on a home network."""
        self.assertNotIn("ChatGPT", WELCOME)


class PostinstallTest(unittest.TestCase):

    def test_it_opens_the_chat_page(self):
        self.assertIn("/chat", POSTINSTALL)

    def test_it_no_longer_opens_the_desktop_app(self):
        """The actual symptom: install finished and the retired app appeared."""
        opens = re.findall(r"/usr/bin/open[^\n]*", POSTINSTALL)
        self.assertTrue(opens, "nothing is opened at all")
        for line in opens:
            self.assertNotIn("-a ", line,
                             f"still launching an application: {line.strip()}")
            self.assertNotIn("DESKTOP_APP", line)

    def test_the_closing_summary_points_at_the_chat_page(self):
        self.assertIn("Talk to it:", POSTINSTALL)
        self.assertNotIn('echo "Desktop app:', POSTINSTALL)

    def test_the_gatekeeper_self_heal_is_still_there(self):
        """Upgrades from installs that shipped the app can still leave a
        renamed folder in /Applications."""
        self.assertIn("localized", POSTINSTALL.lower())

    def test_registration_still_runs(self):
        self.assertIn("--register-mcp", POSTINSTALL)


if __name__ == "__main__":
    unittest.main()
