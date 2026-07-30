"""The onboarding journey: unboxing → signed in → telescope linked → observing.

This is the path a real member walks, and it is the one that must never be
"clunky". Everything here drives the *real* node agent and the *real* cloud
app (ephemeral PostgreSQL) through the actual HTTP calls the Flutter app
makes — no mocks of our own code:

  1. A virgin node agent (no credentials) boots and offers a pairing token.
  2. The app reads that token off the local agent (GET /api/cloud).
  3. The member signs up, then attaches the telescope (POST /me/nodes/attach).
  4. The app hands the credentials to the local agent, both ways it can:
     directly (POST /api/cloud/credentials) and remotely via the pairing
     token (POST /api/v1/nodes/pair, which the agent is polling).
  5. The node reaches "registered", heartbeats, and appears under the
     member's account — ready to observe.

Anything that breaks a step here strands a real person, so each step is
asserted separately with a diagnostic message naming what a user would see.
"""

import json
import time
import unittest
import urllib.error
import urllib.request

from tests.fuzz.faults import FaultPlan
from tests.fuzz.harness import NodeHarness

CLEAN = FaultPlan()          # onboarding must work on healthy hardware


def _cloud_post(url: str, path: str, payload: dict, headers: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


class OnboardingJourneyTest(unittest.TestCase):
    """One boot, walked through the whole journey in order."""

    @classmethod
    def setUpClass(cls):
        cls.h = NodeHarness(CLEAN, scenario_s=5, cloud_mode="real",
                            registered=False).boot()
        cls.cloud_url = cls.h.cloud.url

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_01_agent_offers_a_pairing_token(self):
        """The app cannot start pairing if the agent won't hand out a token."""
        cloud = self.h.get("/api/cloud")
        self.assertTrue(cloud.get("enabled"),
                        "agent reports cloud disabled — app shows nothing to pair")
        self.assertFalse(cloud.get("registered"),
                         "a virgin node must not claim to be registered")
        token = cloud.get("pair_token") or ""
        self.assertRegex(
            token, r"^[A-Z]{4}-\d{4}$",
            f"agent exposed no usable pairing token (got {token!r}) — the app's "
            "Connect telescope screen has nothing to show or send")
        type(self).token = token

    def test_02_member_can_sign_up(self):
        status, body = _cloud_post(
            self.cloud_url, "/api/v1/auth/register",
            {"email": "journey@example.org", "password": "hunter2hunter2",
             "display_name": "Journey"}, {})
        self.assertEqual(status, 200, f"sign-up failed: {body}")
        self.assertTrue(body.get("token"), f"no session token issued: {body}")
        type(self).auth = {"Authorization": f"Bearer {body['token']}"}

    def test_03_attach_returns_installable_credentials(self):
        status, body = _cloud_post(
            self.cloud_url, "/api/v1/me/nodes/attach",
            {"latitude": 31.5, "longitude": -99.2,
             "location_name": "Backyard", "owner_name": "Journey",
             "telescope_model": "ZWO Seestar S50"}, self.auth)
        self.assertEqual(status, 200, f"attach failed: {body}")
        self.assertTrue(body.get("node_id") and body.get("api_key"),
                        f"attach returned no credentials to install: {body}")
        type(self).creds = {"node_id": body["node_id"], "api_key": body["api_key"]}

    def test_04_pairing_push_is_accepted(self):
        """The remote path: app pushes creds to the token the agent polls."""
        status, body = _cloud_post(
            self.cloud_url, "/api/v1/nodes/pair",
            {"pairing_token": self.token, **self.creds}, self.auth)
        self.assertEqual(status, 200,
                         f"cloud rejected the pairing push: {body}")

    def test_05_agent_picks_up_credentials_and_registers(self):
        """The agent must notice within a reasonable wait, unattended."""
        deadline = time.monotonic() + 75
        last = {}
        while time.monotonic() < deadline:
            last = self.h.get("/api/cloud")
            if last.get("registered"):
                break
            time.sleep(2)
        self.assertTrue(
            last.get("registered"),
            "the node never picked up its credentials — a member would sit on "
            f"'waiting to connect' forever. Agent cloud status: {last}")
        self.assertEqual(last.get("node_id"), self.creds["node_id"])

    def test_06_node_heartbeats_and_appears_for_the_member(self):
        deadline = time.monotonic() + 45
        nodes = []
        while time.monotonic() < deadline:
            req = urllib.request.Request(
                self.cloud_url + "/api/v1/me/nodes", headers=self.auth)
            with urllib.request.urlopen(req, timeout=20) as resp:
                nodes = json.loads(resp.read() or b"{}").get("nodes", [])
            if nodes and nodes[0].get("last_heartbeat"):
                break
            time.sleep(2)
        self.assertTrue(nodes, "the linked telescope does not appear under the "
                               "member's account")
        self.assertTrue(
            nodes[0].get("last_heartbeat"),
            "the node never heartbeated after linking — the app shows it "
            f"offline: {nodes[0]}")

    def test_07_a_working_telescope_is_never_reused_by_a_new_link(self):
        """Duplicate-suppression must only ever absorb nodes that never came
        online. Once a telescope has heartbeated it is a real instrument, and
        adding a second telescope must create a second node."""
        status, body = _cloud_post(
            self.cloud_url, "/api/v1/me/nodes/attach",
            {"latitude": 31.5, "longitude": -99.2,
             "location_name": "Backyard", "owner_name": "Journey",
             "telescope_model": "ZWO Seestar S50"}, self.auth)
        self.assertEqual(status, 200, body)
        self.assertNotEqual(
            body["node_id"], self.creds["node_id"],
            "adding a telescope hijacked the one already observing")

    def test_08_agent_reports_ready_to_observe(self):
        """Connect the (fake) telescope and confirm the agent can observe."""
        status, body = self.h.post(
            "/api/connect", {"host": "127.0.0.1", "port": self.h.obs.port})
        self.assertEqual(status, 200, f"telescope connect failed: {body}")
        state = self.h.get("/api/status")
        self.assertTrue(state.get("connected"),
                        f"agent does not consider the telescope connected: {state}")


class RetryDoesNotCreateGhostNodesTest(unittest.TestCase):
    """Linking is retried in the real world; retries must not pile up
    duplicate telescopes on the member's account."""

    @classmethod
    def setUpClass(cls):
        cls.h = NodeHarness(CLEAN, scenario_s=5, cloud_mode="real",
                            registered=False).boot()
        cls.url = cls.h.cloud.url
        _, body = _cloud_post(cls.url, "/api/v1/auth/register",
                              {"email": "ghosts@example.org",
                               "password": "hunter2hunter2"}, {})
        cls.auth = {"Authorization": f"Bearer {body['token']}"}

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def _attach(self):
        status, body = _cloud_post(
            self.url, "/api/v1/me/nodes/attach",
            {"latitude": 40.1, "longitude": -105.2,
             "telescope_model": "ZWO Seestar S50",
             "location_name": "Yard"}, self.auth)
        self.assertEqual(status, 200, body)
        return body["node_id"]

    def test_repeated_attach_reuses_the_same_never_online_node(self):
        ids = {self._attach() for _ in range(4)}
        self.assertEqual(
            len(ids), 1,
            f"four link attempts created {len(ids)} telescopes ({ids}) — a "
            "member retrying a failed link ends up with a list of ghosts")

    def test_member_sees_exactly_one_telescope(self):
        self._attach()
        req = urllib.request.Request(
            self.url + "/api/v1/me/nodes", headers=self.auth)
        with urllib.request.urlopen(req, timeout=20) as resp:
            nodes = json.loads(resp.read() or b"{}").get("nodes", [])
        self.assertEqual(len(nodes), 1,
                         f"expected one telescope on the account, got {len(nodes)}")


class SetupPageTest(unittest.TestCase):
    """The apps tell people to open the node's page; it must exist and show
    the pairing code."""

    @classmethod
    def setUpClass(cls):
        cls.h = NodeHarness(CLEAN, scenario_s=5, cloud_mode="fake",
                            registered=False).boot()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_root_serves_a_setup_page_showing_the_pairing_code(self):
        with urllib.request.urlopen(self.h.base + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200,
                             "http://localhost:5173 must not be a dead link — "
                             "the apps explicitly tell users to open it")
            html = resp.read().decode()
        self.assertIn("Pairing code", html)
        token = self.h.get("/api/cloud").get("pair_token") or ""
        self.assertRegex(token, r"^[A-Z]{4}-\d{4}$")


class DirectInstallPathTest(unittest.TestCase):
    """The desktop app's other route: install credentials straight into the
    local agent, no pairing token round-trip."""

    def test_direct_credential_install_registers_the_node(self):
        h = NodeHarness(CLEAN, scenario_s=5, cloud_mode="real",
                        registered=False).boot()
        try:
            creds = h.cloud.creds     # a node row already exists in the cloud
            status, body = h.post("/api/cloud/credentials", creds)
            self.assertEqual(status, 200,
                             f"agent refused the credentials the app installs: {body}")
            self.assertTrue(body.get("ok"), body)
            cloud = h.get("/api/cloud")
            self.assertTrue(
                cloud.get("registered"),
                f"agent installed credentials but still reports unregistered: {cloud}")
        finally:
            h.stop()


if __name__ == "__main__":
    unittest.main()
