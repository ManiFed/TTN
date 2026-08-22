"""Tool groups registered onto an MCPServer.

Each module exposes `register(server, client)` and is imported by whichever
server can actually serve it: the cloud server registers member/network/
integrity/admin, the local server registers hardware.
"""
