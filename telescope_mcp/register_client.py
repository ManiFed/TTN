#!/usr/bin/env python3
"""Register the node's MCP server with Claude Desktop, so setup has no step 3.

Lives in the package rather than in scripts/ because both installers need it and
neither can rely on a Python interpreter being present: Windows members have
none at all, and the macOS /usr/bin/python3 is deprecated. The packaged agent
exposes it as `TelescopeNetNode --register-mcp`, which is what postinstall.sh
and install.nsi actually call.

Asking a member to hand-edit a JSON config file is the step that loses people:
it is invisible in the installer, easy to get subtly wrong, and produces a
silent failure (the tools simply never appear) with nothing to search for.

The delicate part is that claude_desktop_config.json is not ours. It may
already list other MCP servers the member depends on, so this merges one key
and leaves everything else exactly as it found it. If the file is present but
unparseable, it refuses rather than overwriting -- a corrupt config is the
member's to fix, and replacing it would silently delete their other tools.

    TelescopeNetNode --register-mcp
    TelescopeNetNode --deregister-mcp
    python3 -m telescope_mcp.register_client --command /path/to/agent   # dev
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

#: The key we own inside "mcpServers". Everything else is left untouched.
SERVER_KEY = "telescope-net"


def config_path() -> Path:
    """Claude Desktop's config file for this platform."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if system == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
        return Path(base) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def client_installed() -> bool:
    """Whether Claude Desktop appears to be installed on this machine.

    Registration writes the config either way -- a member who installs Claude
    afterwards should find the telescope already there. But the installer
    should not announce "Claude Desktop can now control this telescope" to
    somebody who has never heard of it, so the message is chosen from this.
    """
    system = platform.system()
    if system == "Darwin":
        return Path("/Applications/Claude.app").exists()
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA") or ""
        return bool(local) and (Path(local) / "AnthropicClaude").exists()
    return (Path.home() / ".local/share/applications/claude.desktop").exists()


def load(path: Path) -> tuple[dict, str | None]:
    """Existing config, or an error explaining why we must not touch it."""
    if not path.exists():
        return {}, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"Could not read {path}: {exc}"
    if not text.strip():
        return {}, None
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, (
            f"{path} is not valid JSON ({exc}). Refusing to touch it — it may "
            f"list other MCP servers, and rewriting it would delete them. Fix "
            f"the file by hand, then re-run this."
        )
    if not isinstance(data, dict):
        return {}, f"{path} does not contain a JSON object. Refusing to touch it."
    return data, None


def write(path: Path, data: dict) -> None:
    """Write atomically, keeping one backup of whatever was there before."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".backup"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)          # atomic: never a half-written config


def entry(command: str, data_dir: str, prefix_args: list[str] | None = None) -> dict:
    """The config entry that launches this agent in MCP mode.

    `prefix_args` covers running from a source checkout, where `command` is a
    bare Python interpreter and needs `-m src.main_service` before the flags.
    In a packaged build `command` is the agent itself and there is no prefix.
    """
    args = list(prefix_args or []) + ["--mcp"]
    if data_dir:
        args += ["--data-dir", data_dir]
    return {"command": command, "args": args}


def register(command: str, data_dir: str, path: Path,
             prefix_args: list[str] | None = None) -> tuple[bool, str]:
    data, error = load(path)
    if error:
        return False, error

    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        return False, (f"'mcpServers' in {path} is not an object. Refusing to "
                       f"touch it.")

    desired = entry(command, data_dir, prefix_args)
    if servers.get(SERVER_KEY) == desired:
        return True, f"Already registered in {path} — nothing to change."

    existing = sorted(k for k in servers if k != SERVER_KEY)
    servers[SERVER_KEY] = desired
    data["mcpServers"] = servers
    try:
        write(path, data)
    except OSError as exc:
        return False, f"Could not write {path}: {exc}"

    kept = (f" Left {len(existing)} other MCP server(s) untouched: "
            f"{', '.join(existing)}." if existing else "")
    waiting = ("" if client_installed() else
               " Claude Desktop is not installed yet; the telescope will be "
               "there when it is.")
    return True, f"Registered '{SERVER_KEY}' in {path}.{kept}{waiting}"


def remove(path: Path) -> tuple[bool, str]:
    data, error = load(path)
    if error:
        return False, error
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_KEY not in servers:
        return True, f"Not registered in {path} — nothing to remove."
    servers.pop(SERVER_KEY)
    data["mcpServers"] = servers
    try:
        write(path, data)
    except OSError as exc:
        return False, f"Could not write {path}: {exc}"
    return True, f"Removed '{SERVER_KEY}' from {path}."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--command", default="",
                        help="executable to launch (the packaged node agent)")
    parser.add_argument("--data-dir", default="",
                        help="the agent's data directory, passed through as --data-dir")
    parser.add_argument("--config", default="", help="override the config path")
    parser.add_argument("--remove", action="store_true", help="deregister instead")
    args = parser.parse_args()

    path = Path(args.config) if args.config else config_path()

    if args.remove:
        ok, message = remove(path)
    else:
        command = args.command or sys.executable
        ok, message = register(command, args.data_dir, path)

    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
