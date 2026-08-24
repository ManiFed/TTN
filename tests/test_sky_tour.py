import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from telescope_mcp.client import AgentClient
from telescope_mcp.local_server import build_server


def call(server, args):
    result = asyncio.run(server.call_tool("sky_tour", args))
    return "".join(getattr(c, "text", "") for c in result.content)


class SkyTourTest(unittest.TestCase):
    def setUp(self):
        self.agent = MagicMock(spec=AgentClient)
        self.agent.post.return_value = {}
        self.server = build_server(self.agent, with_cloud=False)

    def test_preview_never_moves_and_has_narration(self):
        text = call(self.server, {"action": "preview"})
        self.agent.post.assert_not_called()
        self.assertIn("Orion Nebula", text)
        self.assertIn("Preview only", text)

    def test_start_then_next_moves_one_stop_each_time(self):
        with patch.dict(os.environ, {"TELESCOPE_MCP_ENV": "sim"}, clear=True):
            first = call(self.server, {"action": "start"})
            second = call(self.server, {"action": "next"})
        self.assertIn("Orion Nebula", first)
        self.assertIn("Pleiades", second)
        self.assertEqual([c.args[0] for c in self.agent.post.call_args_list],
                         ["/api/slew", "/api/slew"])

    def test_next_requires_a_started_tour(self):
        text = call(self.server, {"action": "next"})
        self.agent.post.assert_not_called()
        self.assertIn("No sky tour is active", text)


if __name__ == "__main__":
    unittest.main()
