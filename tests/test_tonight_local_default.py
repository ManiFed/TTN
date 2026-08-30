"""Local tonight_accept must use this computer's node, not a sibling.

Starfront 2026-08-29: Claude called tonight_accept on stale seestar-live-001
while this Mac's agent was node_600334db. Issue #41.
"""
import unittest
from unittest.mock import MagicMock

from telescope_mcp.client import AgentClient, CloudClient
from telescope_mcp.local_server import build_server
from telescope_mcp.tools import tonight


def _call(server, name, args=None):
    import asyncio
    result = asyncio.run(server.call_tool(name, args or {}))
    text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []))
    return text, result


class TonightLocalDefaultTest(unittest.TestCase):

    def setUp(self):
        self.agent = MagicMock(spec=AgentClient)
        self.client = MagicMock(spec=CloudClient)
        self.client.base = "https://example.invalid"
        self.client.authenticated = True
        self.agent.get.return_value = {
            "registered": True, "node_id": "node_600334db",
        }
        self.client.post.return_value = {
            "node_id": "node_600334db", "status": "accepted",
            "observing": True, "proposal": {"research_hours": 4},
        }
        self.client.get.return_value = {"nodes": []}
        self.server = build_server(self.agent, self.client)

    def test_omitted_node_id_uses_this_computers_identity(self):
        _call(self.server, "tonight_accept", {})
        path = self.client.post.call_args[0][0]
        self.assertIn("node_600334db", path)
        self.assertNotIn("seestar-live-001", path)

    def test_explicit_node_id_is_honoured(self):
        _call(self.server, "tonight_accept", {"node_id": "node_other"})
        path = self.client.post.call_args[0][0]
        self.assertIn("node_other", path)

    def test_remote_mcp_without_agent_refuses_a_blank_id(self):
        from mcp.server import MCPServer
        server = MCPServer(
            name="telescope-net", title="t", instructions="t", version="0.1.0",
        )
        tonight.register(server, self.client, None)
        self.client.post.reset_mock()
        text, _ = _call(server, "tonight_accept", {})
        self.assertIn("telescope computer", text.lower())
        self.client.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
