#!/usr/bin/env python3
"""Every capability the Flutter app has must be reachable from the MCP servers.

The MCP servers were added so the product could be driven through chat rather
than only through the desktop app. That promise decays silently: someone adds
a method to ApiClient for a new screen, the servers never grow the matching
tool, and the chat interface quietly falls behind without any test failing.

So this test reads the app's own API clients, maps each method to the tool
that carries it, and fails in both directions:

  * a Dart method with no entry in the map  -> the app grew a capability and
    the MCP surface has not caught up
  * an entry naming a tool that is not registered -> the map is lying

Run with:  python3 -m pytest tests/test_mcp_parity.py
"""

import asyncio
import re
import unittest
from pathlib import Path

from telescope_mcp.cloud_server import build_server as build_cloud_server
from telescope_mcp.local_server import build_server as build_local_server

REPO = Path(__file__).resolve().parents[1]
API_CLIENT = REPO / "app" / "lib" / "api" / "api_client.dart"
AGENT_CLIENT = REPO / "app" / "lib" / "api" / "node_agent_client.dart"

#: Dart method on ApiClient -> MCP tool(s) that carry it.
#: A tuple carries a capability that needed more than one tool; an empty tuple
#: means "deliberately not exposed", and must say why.
CLOUD_PARITY: dict[str, tuple[str, ...]] = {
    # auth
    "register":                ("auth_login",),   # sign-up stays in the app; see NOT_EXPOSED
    "login":                   ("auth_login",),
    "logout":                  ("auth_logout",),
    # member data
    "me":                      ("member_me",),
    "stats":                   ("member_stats",),
    "nodes":                   ("member_list_nodes",),
    "claimNode":               ("member_claim_node",),
    "observations":            ("member_observations",),
    "timeline":                ("member_timeline",),
    "notifications":           ("member_notifications",),
    "markNotificationRead":    ("member_mark_notification_read",),
    "telescopes":              ("list_telescope_specs",),
    "latestVersion":           ("latest_version",),
    "attachNode":              ("member_attach_node",),
    "startNodeSession":        ("member_start_session",),
    "endNodeSession":          ("member_end_session",),
    "setNodeVacation":         ("member_set_vacation",),
    "cancelNodeVacation":      ("member_cancel_vacation",),
    "setNodeDryRun":           ("admin_start_dry_run",),
    "clearNodeDryRun":         ("admin_stop_dry_run",),
    "disconnectNode":          ("member_disconnect_node",),
    "updateNodeDisplayName":   ("member_rename_node",),
    "skyQuality":              ("sky_quality",),
    "liveFleet":               ("network_live_fleet",),
    "discoveries":             ("member_discoveries",),
    "setNotificationPrefs":    ("member_notification_prefs",),
    "deleteAccount":           ("member_delete_account",),
    "nights":                  ("member_nights",),
    "targets":                 ("network_targets",),
    "lightCurve":              ("target_light_curve",),
    "objectDetails":           ("target_details",),
    "suggestScienceProgram":   ("suggest_science_program",),
    "helpSession":             ("help_session",),
    "helpChat":                ("help_chat",),
    # deliberately not exposed
    "pushPairCredentials":     (),
    "close":                   (),
}

#: Dart method on NodeAgentClient -> local MCP tool(s).
LOCAL_PARITY: dict[str, tuple[str, ...]] = {
    "status":                ("node_status",),
    "identity":              ("node_identity",),
    "installCredentials":    ("node_install_credentials",),
    "discoverAlpacaServers": ("node_discover_alpaca",),
    "connectAlpaca":         ("node_connect_alpaca",),
}

#: Why a capability has no tool. Every empty tuple above needs an entry here.
NOT_EXPOSED = {
    "register": (
        "Account creation takes a password. Handling one through a chat "
        "transcript would put a credential in the conversation, so sign-up "
        "stays in the app and the browser."
    ),
    "pushPairCredentials": (
        "Pushes a node api_key to a pairing agent. Exposing it would mean a "
        "tool argument carrying a live credential."
    ),
    "close": "Transport teardown, not a product capability.",
}

#: Dart Object overrides and private helpers — not product capabilities.
_DART_OVERRIDES = {"toString", "hashCode", "noSuchMethod", "operator"}
_PRIVATE_OR_PLUMBING = re.compile(r"^_")

#: A class member declared at exactly two spaces of indent: return type (which
#: may carry nested generics like Future<List<Map<String, dynamic>>>, records
#: like (List<X>, int), and nullability) followed by the method name.
_METHOD_RE = re.compile(
    r"^\s{2}(?:static\s+)?"
    r"[A-Za-z_][A-Za-z0-9_<>,\s.?()]*?\s+"
    r"([a-z][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)


def dart_methods(path: Path) -> set[str]:
    """Public method names declared on the client class in `path`."""
    source = path.read_text()
    found = {m.group(1) for m in _METHOD_RE.finditer(source)}
    return {n for n in found
            if not _PRIVATE_OR_PLUMBING.match(n) and n not in _DART_OVERRIDES}


def tool_names(server) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


class McpParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud_tools = tool_names(build_cloud_server())
        cls.local_tools = tool_names(build_local_server())

    # ── the app has not outgrown the MCP surface ──────────────────────────

    def test_every_api_client_method_is_mapped(self):
        missing = dart_methods(API_CLIENT) - set(CLOUD_PARITY)
        self.assertFalse(missing, (
            f"ApiClient gained {sorted(missing)} with no MCP tool. Add the tool "
            f"in telescope_mcp/tools/, then map it in CLOUD_PARITY — or map it "
            f"to () and say why in NOT_EXPOSED."
        ))

    def test_every_agent_client_method_is_mapped(self):
        missing = dart_methods(AGENT_CLIENT) - set(LOCAL_PARITY)
        self.assertFalse(missing, (
            f"NodeAgentClient gained {sorted(missing)} with no MCP tool. Add it "
            f"to telescope_mcp/tools/hardware.py and map it in LOCAL_PARITY."
        ))

    # ── the map is not lying ──────────────────────────────────────────────

    def test_mapped_cloud_tools_exist(self):
        for method, tools in CLOUD_PARITY.items():
            for tool in tools:
                self.assertIn(tool, self.cloud_tools,
                              f"CLOUD_PARITY maps {method} -> {tool}, which is "
                              f"not registered on the cloud server.")

    def test_mapped_local_tools_exist(self):
        for method, tools in LOCAL_PARITY.items():
            for tool in tools:
                self.assertIn(tool, self.local_tools,
                              f"LOCAL_PARITY maps {method} -> {tool}, which is "
                              f"not registered on the local server.")

    def test_unexposed_capabilities_are_justified(self):
        for mapping in (CLOUD_PARITY, LOCAL_PARITY):
            for method, tools in mapping.items():
                if not tools:
                    self.assertIn(method, NOT_EXPOSED,
                                  f"{method} is mapped to no tool but NOT_EXPOSED "
                                  f"does not say why.")

    # ── the parser is actually finding methods ────────────────────────────

    def test_parser_finds_the_expected_clients(self):
        """A regex that silently matched nothing would make parity vacuous."""
        self.assertGreater(len(dart_methods(API_CLIENT)), 25)
        self.assertGreaterEqual(len(dart_methods(AGENT_CLIENT)), 5)

    # ── tool hygiene ──────────────────────────────────────────────────────

    def test_every_tool_has_a_description(self):
        for server, label in ((build_cloud_server(), "cloud"),
                              (build_local_server(), "local")):
            for tool in asyncio.run(server.list_tools()):
                self.assertTrue(
                    (tool.description or "").strip(),
                    f"{label} tool {tool.name} has no description; the model has "
                    f"nothing to go on when deciding whether to call it.")

    def test_node_server_is_a_superset_of_the_cloud_server(self):
        """Someone at the telescope installs one server and gets everything.

        Linking a telescope spans both backends — LAN discovery is local,
        credentials come from the cloud, and the credential is written back to
        the agent. If the node server ever stops carrying the cloud tools, that
        flow silently becomes impossible to complete in one place.
        """
        missing = self.cloud_tools - self.local_tools
        self.assertFalse(missing, (
            f"The node server is missing {sorted(missing)}, which the cloud "
            f"server has. It must register every cloud tool as well as its own."))
        self.assertTrue(self.local_tools - self.cloud_tools,
                        "The node server should also add its own node_* tools.")


if __name__ == "__main__":
    unittest.main()
