"""MCP servers exposing the Telescope Net through a chat interface.

Two servers share one tool vocabulary:

  cloud_server.py  — wraps the member/admin API at api.thetelescope.net
                     (cloud/server.py). Runs over stdio for local clients or
                     streamable-http so it can be added as a remote connector.
  local_server.py  — wraps the node agent's localhost API (src/dashboard.py)
                     on the machine physically attached to a telescope.

Every tool is a thin call onto an endpoint the Flutter app already uses, so
the two interfaces cannot drift in behaviour — only in presentation. See
tests/test_mcp_parity.py, which fails when the app grows a call the servers
do not expose.
"""

__all__ = ["client", "guard"]
