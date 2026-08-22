#!/usr/bin/env python3
"""Thin wrapper kept for development; the logic lives in the package.

The installers call `TelescopeNetNode --register-mcp` instead, because neither
Windows nor a future macOS can be relied on to have a Python interpreter.
"""

import sys

from telescope_mcp.register_client import main

if __name__ == "__main__":
    sys.exit(main())
