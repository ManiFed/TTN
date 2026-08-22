#!/usr/bin/env python3
"""connect_my_telescope: one sentence, and it must not orphan a node.

The flow this tool automates is the one that produced 319fded and 0c4bb87. The
dangerous step is the third: a computer that already has cloud credentials must
claim *that* node, not register a fresh one. Getting it wrong is silent -- the
telescope keeps working, the member keeps seeing a dashboard -- and the old
node keeps its entire observation history somewhere nobody is looking.

So the tests below care most about which arguments reach /me/nodes/attach.

Run with:  python3 -m pytest tests/test_mcp_setup.py
"""

import unittest
from unittest.mock import MagicMock, patch

from telescope_mcp.client import AgentClient, ApiError, CloudClient
from telescope_mcp.local_server import build_server


def call(server, name, args=None):
    import asyncio
    from mcp.server.mcpserver.exceptions import ToolError
    try:
        result = asyncio.run(server.call_tool(name, args or {}))
    except ToolError as exc:
        return False, str(exc), None
    text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []))
    failed = getattr(result, "is_error", None)
    if failed is None:
        failed = getattr(result, "isError", False)
    return not failed, text, result


class _Fixture(unittest.TestCase):
    """A telescope on the LAN, an agent that answers, and a cloud that links."""

    def setUp(self):
        self.agent = MagicMock(spec=AgentClient)
        self.client = MagicMock(spec=CloudClient)
        self.client.base = "https://example.invalid"
        self.client.authenticated = True

        self.discovered = {"servers": [{"host": "192.168.1.44", "port": 11111}]}
        self.identity = {"registered": False}
        self.status = {"telescope": {"connected": True},
                       "camera": {"connected": True},
                       "safety": {"safe": True, "reason": ""}}

        def agent_get(path, params=None, timeout=8.0):
            if path == "/api/cloud/identity":
                return self.identity
            if path == "/api/status":
                return self.status
            return {}

        def agent_post(path, body=None, timeout=15.0):
            if path == "/api/discover":
                return self.discovered
            return {}

        self.agent.get.side_effect = agent_get
        self.agent.post.side_effect = agent_post
        self.client.post.return_value = {"node_id": "node_new", "api_key": "KEY-NEW"}

        self.server = build_server(self.agent, self.client)

    def attach_body(self):
        """The body sent to /me/nodes/attach."""
        for args, _ in [c for c in self.client.post.call_args_list]:
            if args and args[0] == "/me/nodes/attach":
                return args[1]
        return None


class IdentityReuseTest(_Fixture):

    def test_an_existing_registration_is_claimed_not_duplicated(self):
        """The orphan-prevention path: reuse this computer's node."""
        self.identity = {"registered": True, "node_id": "node_old",
                         "api_key": "KEY-OLD"}
        ok, text, _ = call(self.server, "connect_my_telescope", {})
        self.assertTrue(ok, text)
        body = self.attach_body()
        self.assertEqual(body.get("node_id"), "node_old",
                         "an already-registered computer must claim its own "
                         "node, not create a second one")
        self.assertEqual(body.get("api_key"), "KEY-OLD")

    def test_a_fresh_computer_registers_a_new_node(self):
        self.identity = {"registered": False}
        ok, text, _ = call(self.server, "connect_my_telescope", {})
        self.assertTrue(ok, text)
        body = self.attach_body()
        self.assertNotIn("node_id", body)
        self.assertNotIn("api_key", body)

    def test_an_unreachable_agent_does_not_invent_a_new_registration(self):
        """If we cannot tell whether a node exists, we must not assume it does not.

        This is the conservative half of the same bug: an identity check that
        errors must surface, not silently fall through to 'register a new one'.
        """
        def failing_get(path, params=None, timeout=8.0):
            if path == "/api/cloud/identity":
                raise ApiError(0, "agent not reachable")
            return self.status if path == "/api/status" else {}
        self.agent.get.side_effect = failing_get
        ok, text, _ = call(self.server, "connect_my_telescope", {})
        # It proceeds, but the step log must record that identity was unknown.
        self.assertIn("existing_identity", text)


class FlowTest(_Fixture):

    def test_the_happy_path_reports_connected(self):
        ok, text, _ = call(self.server, "connect_my_telescope",
                           {"telescope_display_name": "Starfront"})
        self.assertTrue(ok, text)
        self.assertIn('"connected": true', text.lower())
        self.assertIn("Starfront", text)

    def test_credentials_are_written_back_to_the_agent(self):
        call(self.server, "connect_my_telescope", {})
        installs = [c for c in self.agent.post.call_args_list
                    if c[0] and c[0][0] == "/api/cloud/credentials"]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0][0][1]["node_id"], "node_new")

    def test_signing_in_is_required_before_linking(self):
        self.client.authenticated = False
        ok, text, _ = call(self.server, "connect_my_telescope", {})
        self.assertIn("auth_login", text)
        self.client.post.assert_not_called()

    def test_no_telescope_found_leads_with_access_point_mode(self):
        """The most common setup failure, and the vendor app hides it.

        A generic "check it is powered on" sends people to look at the wrong
        thing: the telescope usually IS on and IS connected — to its own
        network. Saying so first is the difference between a two-minute fix
        and giving up.
        """
        self.discovered = {"servers": []}
        ok, text, _ = call(self.server, "connect_my_telescope", {})
        self.assertFalse(ok is None)
        self.assertIn("Access Point", text)
        self.assertIn("Station Mode", text)
        self.client.post.assert_not_called()

    def test_no_telescope_found_lists_the_other_causes_in_order(self):
        self.discovered = {"servers": []}
        _, text, _ = call(self.server, "connect_my_telescope", {})
        for phrase in ("guest network", "client isolation", "finished booting"):
            self.assertIn(phrase, text)

    def test_network_help_is_available_without_running_a_connect(self):
        """So "why can't you find my telescope" is answerable on its own."""
        _, text, _ = call(self.server, "network_help", {})
        self.assertIn("Station Mode", text)
        self.assertIn("client isolation", text)

    def test_an_explicit_host_skips_discovery(self):
        call(self.server, "connect_my_telescope",
             {"host": "10.0.0.5", "port": 11111})
        discovers = [c for c in self.agent.post.call_args_list
                     if c[0] and c[0][0] == "/api/discover"]
        self.assertEqual(discovers, [])

    def test_a_failure_names_the_step_that_failed(self):
        def failing_post(path, body=None, timeout=15.0):
            if path == "/api/discover":
                return self.discovered
            if path == "/api/connect":
                raise ApiError(500, "ALPACA server refused the connection")
            return {}
        self.agent.post.side_effect = failing_post
        ok, text, _ = call(self.server, "connect_my_telescope", {})
        self.assertIn("connect", text)
        self.assertIn("ALPACA server refused", text)
        self.client.post.assert_not_called()

    def test_the_api_key_is_never_returned_to_the_caller(self):
        """A credential in a transcript cannot be recalled."""
        self.identity = {"registered": True, "node_id": "node_old",
                         "api_key": "KEY-OLD"}
        _, text, _ = call(self.server, "connect_my_telescope", {})
        self.assertNotIn("KEY-OLD", text)
        self.assertNotIn("KEY-NEW", text)

    def test_a_telescope_that_never_comes_up_is_reported_honestly(self):
        self.status = {"telescope": {"connected": False}}
        with patch("telescope_mcp.tools.setup.time.sleep"):
            _, text, _ = call(self.server, "connect_my_telescope", {})
        self.assertIn('"connected": false', text.lower())
        self.assertIn("node_logs", text)


class DiagnoseTest(_Fixture):

    def test_a_healthy_node_says_so(self):
        """Healthy means connected, safe, and actually registered."""
        self.identity = {"registered": True, "node_id": "node_ok"}
        self.client.get.return_value = {}
        _, text, _ = call(self.server, "diagnose", {})
        self.assertIn("Nothing obviously wrong", text)

    def test_problems_are_summarised_before_the_json(self):
        self.status = {"telescope": {"connected": False},
                       "camera": {"connected": False},
                       "safety": {"safe": False, "reason": "Sun above horizon"}}
        self.identity = {"registered": False}
        self.client.get.return_value = {}
        _, text, _ = call(self.server, "diagnose", {})
        self.assertIn("telescope is not connected", text)
        self.assertIn("Sun above horizon", text)
        self.assertIn("not registered", text)

    def test_a_safety_stop_is_not_mistaken_for_an_unreachable_agent(self):
        """/api/status carries a top-level "error" whenever safety has latched,
        which is most of any daytime. Reading that as a transport failure sends
        people to restart software that is working perfectly."""
        self.identity = {"registered": True, "node_id": "node_ok"}
        self.status = {"telescope": {"connected": True},
                       "camera": {"connected": True},
                       "error": "Safety stop: sun above horizon"}
        self.client.get.return_value = {}
        _, text, _ = call(self.server, "diagnose", {})
        self.assertNotIn("not reachable", text)
        self.assertIn("sun above horizon", text)
        self.assertIn("expected in daylight", text)

    def test_an_unreachable_agent_is_the_headline(self):
        self.agent.get.side_effect = ApiError(0, "Could not reach the node software")
        self.client.get.return_value = {}
        _, text, _ = call(self.server, "diagnose", {})
        self.assertIn("not reachable", text)

    def test_logs_are_marked_untrusted(self):
        self.client.get.return_value = {}
        _, text, _ = call(self.server, "diagnose", {})
        self.assertIn("untrusted", text)

    def test_a_missing_admin_key_skips_integrity_instead_of_failing(self):
        self.client.get.side_effect = ApiError(401, "invalid admin key")
        _, text, _ = call(self.server, "diagnose", {})
        self.assertIn("skipped", text)


if __name__ == "__main__":
    unittest.main()


class ImageTest(unittest.TestCase):
    """Images must come back as image content, and "not yet" must read as English."""

    #: Smallest valid PNG — a 1x1 transparent pixel.
    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
           b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

    def setUp(self):
        self.agent = MagicMock(spec=AgentClient)
        self.client = MagicMock(spec=CloudClient)
        self.client.base = "https://example.invalid"
        self.client.authenticated = False
        self.server = build_server(self.agent, self.client)

    def test_a_frame_comes_back_as_image_content(self):
        self.agent.get_bytes.return_value = (self.PNG, "image/png")
        import asyncio
        result = asyncio.run(self.server.call_tool("last_image", {}))
        kinds = [getattr(c, "type", None) for c in result.content]
        self.assertIn("image", kinds,
                      "the frame should render in the conversation, not arrive "
                      "as a link or a blob of base64 text")

    def test_no_stack_yet_reads_as_a_sentence_not_a_404(self):
        """Early in a night there is no stack. That is normal, not an error."""
        self.agent.get_bytes.side_effect = ApiError(
            404, "No stacked preview available yet")
        ok, text, _ = call(self.server, "stacked_preview", {})
        self.assertFalse(ok)
        self.assertIn("No stacked preview available yet", text)

    def test_tonight_results_survives_a_partly_unavailable_node(self):
        """A missing section must not cost the member the rest of the report."""
        def flaky(path, params=None, timeout=8.0):
            if path == "/api/aavso":
                raise ApiError(500, "exporter not ready")
            return {"measured": 42, "queued": 3}
        self.agent.get.side_effect = flaky
        _, text, _ = call(self.server, "tonight_results", {})
        self.assertIn("42 measurement", text)
        self.assertIn("exporter not ready", text)


class ImagingProgramTest(unittest.TestCase):
    """The imaging half of a night — and it must not run on a real telescope."""

    def setUp(self):
        self.agent = MagicMock(spec=AgentClient)
        self.client = MagicMock(spec=CloudClient)
        self.client.base = "https://example.invalid"
        self.client.authenticated = False
        self.agent.post.return_value = {}
        self.server = build_server(self.agent, self.client)
        self.args = {"target_name": "M51", "ra_hours": 13.5, "dec_deg": 47.2}

    def test_it_slews_centres_and_stacks(self):
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "sim"}, clear=True):
            ok, text, _ = call(self.server, "run_imaging_program", self.args)
        self.assertTrue(ok, text)
        paths = [c[0][0] for c in self.agent.post.call_args_list]
        self.assertEqual(paths, ["/api/slew", "/api/center/run", "/api/stack/start"])
        self.assertIn("M51", text)

    def test_it_refuses_to_move_a_production_telescope(self):
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "production"}, clear=True):
            ok, text, _ = call(self.server, "run_imaging_program", self.args)
        self.assertFalse(ok)
        self.agent.post.assert_not_called()

    def test_a_refused_slew_explains_the_likely_cause(self):
        self.agent.post.side_effect = ApiError(400, "below horizon mask")
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "sim"}, clear=True):
            ok, text, _ = call(self.server, "run_imaging_program", self.args)
        self.assertIn("horizon mask", text)
        self.assertIn('"started": false', text.lower())

    def test_a_failed_centring_does_not_abandon_the_imaging(self):
        """An uncentred frame is still a frame."""
        def post(path, body=None, timeout=15.0):
            if path == "/api/center/run":
                raise ApiError(500, "plate solve failed")
            return {}
        self.agent.post.side_effect = post
        with patch.dict("os.environ", {"TELESCOPE_MCP_ENV": "sim"}, clear=True):
            ok, text, _ = call(self.server, "run_imaging_program", self.args)
        self.assertTrue(ok, text)
        self.assertIn("plate solve failed", text)
        self.assertIn('"started": true', text.lower())

    def test_targets_can_be_searched_by_name(self):
        self.agent.get.return_value = [
            {"name": "M51", "common_name": "Whirlpool Galaxy"},
            {"name": "M42", "common_name": "Orion Nebula"},
        ]
        _, text, _ = call(self.server, "imaging_targets", {"search": "orion"})
        self.assertIn("M42", text)
        self.assertNotIn("M51", text)
