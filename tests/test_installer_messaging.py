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


BUILD_DMG = (REPO / "build/macos/build_dmg.sh").read_text()
RELEASE = (REPO / ".github/workflows/release.yml").read_text()


class NoDesktopAppTest(unittest.TestCase):
    """The desktop app is retired, so nothing should build, sign, stage or
    publish it. Two control surfaces in /Applications is worse than one, and
    the one being shipped would be the one we are moving away from."""

    def test_the_package_does_not_stage_the_app(self):
        for marker in ("HAS_DESKTOP_APP", "FLUTTER_APP_SOURCE",
                       "DESKTOP_BUNDLE_DIR"):
            self.assertNotIn(marker, BUILD_DMG)

    def test_the_build_no_longer_fails_without_a_flutter_app(self):
        """It used to exit 1 if the app was missing, which would now break
        every release."""
        self.assertNotIn("Flutter member app not found", BUILD_DMG)

    def test_the_release_does_not_build_flutter(self):
        """The job, not the word -- the header comment legitimately explains
        why the app is no longer built."""
        import yaml
        jobs = yaml.safe_load(RELEASE)["jobs"]
        self.assertNotIn("flutter-macos", jobs)
        for name, job in jobs.items():
            steps = yaml.dump(job.get("steps", []))
            self.assertNotIn("flutter build", steps, f"{name} still builds it")
            self.assertNotIn("subosito/flutter-action", steps)

    def test_the_release_publishes_only_the_installer(self):
        self.assertNotIn("TelescopeNet-macos.app.zip", RELEASE)

    def test_the_node_agent_is_still_built_and_shipped(self):
        self.assertIn("TelescopeNetNode", BUILD_DMG)
        self.assertIn("node-agent-macos", RELEASE)

    def test_the_build_points_at_the_chat_page(self):
        self.assertIn("/chat", BUILD_DMG)


class OpeningTest(unittest.TestCase):
    """What the installer opens, and when.

    A real install failed both ways at once: it opened the chat page for
    somebody who already had Claude Desktop, and opened it before the agent
    had bound its port, so the browser showed a refused connection.
    """

    def test_it_waits_for_the_agent_before_opening_anything(self):
        self.assertIn("AGENT_READY", POSTINSTALL)
        wait = POSTINSTALL.index("AGENT_READY=0")
        open_at = POSTINSTALL.index("/usr/bin/open")
        self.assertLess(wait, open_at, "it opens before it waits")

    def test_the_wait_is_long_enough_for_a_first_run(self):
        """Ten seconds was not: a fresh unsigned bundle pays Gatekeeper
        verification and a one-off unpack before any of our code runs."""
        self.assertIn("seq 1 60", POSTINSTALL)

    def test_it_never_opens_a_page_that_is_not_being_served(self):
        """A refused connection reads as "the install failed"."""
        guard = 'if [ "${AGENT_READY}" -eq 1 ]'
        self.assertIn(guard, POSTINSTALL)
        self.assertLess(POSTINSTALL.index(guard),
                        POSTINSTALL.index("/usr/bin/open"))

    def test_it_does_not_open_the_page_when_an_assistant_is_installed(self):
        """They have just had their telescope registered with it; a second
        unfamiliar chat window on top of that is noise."""
        self.assertIn('[ "${HAS_ASSISTANT}" -eq 0 ]', POSTINSTALL)

    def test_the_assistant_check_happens_before_it_is_used(self):
        self.assertLess(POSTINSTALL.index("HAS_ASSISTANT=0"),
                        POSTINSTALL.index('[ "${HAS_ASSISTANT}" -eq 0 ]'))

    def test_it_says_what_to_do_if_the_agent_never_came_up(self):
        self.assertIn("still", POSTINSTALL)
        self.assertIn("/chat", POSTINSTALL)
