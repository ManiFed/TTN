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


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")


def _home(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


#: Every MCP client we know how to set up, and where each keeps its config.
#: A client is registered whether or not it is installed -- someone who adds
#: one later should find the telescope already there -- so this is a map of
#: where to write, not a list of what is present.
#:
#: They all read the same {"mcpServers": {...}} shape, which is why one entry
#: serves all of them. The only real differences are the path and, for the
#: editors, that the file is shared with a great deal of unrelated settings --
#: which is exactly why register() merges rather than writes.
CLIENTS: dict[str, dict[str, Path]] = {
    "Claude Desktop": {
        "Darwin": _home("Library/Application Support/Claude/claude_desktop_config.json"),
        "Windows": _appdata() / "Claude/claude_desktop_config.json",
        "Linux": _home(".config/Claude/claude_desktop_config.json"),
    },
    "ChatGPT Desktop": {
        "Darwin": _home("Library/Application Support/ChatGPT/mcp_config.json"),
        "Windows": _appdata() / "OpenAI/ChatGPT/mcp_config.json",
        "Linux": _home(".config/ChatGPT/mcp_config.json"),
    },
    "Cursor": {
        "Darwin": _home(".cursor/mcp.json"),
        "Windows": _home(".cursor/mcp.json"),
        "Linux": _home(".cursor/mcp.json"),
    },
    "Windsurf": {
        "Darwin": _home(".codeium/windsurf/mcp_config.json"),
        "Windows": _home(".codeium/windsurf/mcp_config.json"),
        "Linux": _home(".codeium/windsurf/mcp_config.json"),
    },
    "Claude Code": {
        "Darwin": _home(".claude.json"),
        "Windows": _home(".claude.json"),
        "Linux": _home(".claude.json"),
    },
}

#: How to tell whether a client is actually installed, so the installer can say
#: something true rather than announcing a tool nobody has.
_PRESENCE: dict[str, dict[str, Path]] = {
    "Claude Desktop": {
        "Darwin": Path("/Applications/Claude.app"),
        "Windows": Path(os.environ.get("LOCALAPPDATA") or "") / "AnthropicClaude",
        "Linux": _home(".local/share/applications/claude.desktop"),
    },
    "ChatGPT Desktop": {
        "Darwin": Path("/Applications/ChatGPT.app"),
        "Windows": Path(os.environ.get("LOCALAPPDATA") or "") / "OpenAI/ChatGPT",
        "Linux": _home(".local/share/applications/chatgpt.desktop"),
    },
    "Cursor": {
        "Darwin": Path("/Applications/Cursor.app"),
        "Windows": Path(os.environ.get("LOCALAPPDATA") or "") / "Programs/Cursor",
        "Linux": _home(".local/share/applications/cursor.desktop"),
    },
    "Windsurf": {
        "Darwin": Path("/Applications/Windsurf.app"),
        "Windows": Path(os.environ.get("LOCALAPPDATA") or "") / "Programs/Windsurf",
        "Linux": _home(".local/share/applications/windsurf.desktop"),
    },
    "Claude Code": {
        "Darwin": _home(".claude.json"),
        "Windows": _home(".claude.json"),
        "Linux": _home(".claude.json"),
    },
}


def config_path(client: str = "Claude Desktop") -> Path:
    """Where `client` keeps its MCP configuration on this platform."""
    system = platform.system()
    paths = CLIENTS.get(client) or CLIENTS["Claude Desktop"]
    return paths.get(system) or paths["Linux"]


def installed_clients() -> list[str]:
    """Which supported assistants are actually on this machine."""
    system = platform.system()
    found = []
    for name, paths in _PRESENCE.items():
        probe = paths.get(system) or paths.get("Linux")
        try:
            if probe and str(probe) != "" and probe.exists():
                found.append(name)
        except OSError:
            continue
    return found


def client_installed() -> bool:
    """Whether any supported assistant is installed."""
    return bool(installed_clients())


def register_all(command: str, data_dir: str,
                 prefix_args: list[str] | None = None) -> tuple[bool, str]:
    """Register with every supported assistant.

    Writes to all of them rather than only the ones present: an entry costs
    nothing, and someone who installs an assistant next week should find their
    telescope already there rather than having to re-run an installer they have
    long since thrown away.

    Succeeds if any client took it. One unwritable config -- a locked-down
    editor install, an unparseable file we must not touch -- is reported but
    does not fail the whole thing.
    """
    done, failed = [], []
    for name in CLIENTS:
        ok, message = register(command, data_dir, config_path(name), prefix_args)
        (done if ok else failed).append(f"{name}: {message}")

    present = installed_clients()
    lines = [f"Registered with {len(done)} assistant config(s)."]
    if present:
        lines.append("Installed here: " + ", ".join(present) + ".")
    else:
        lines.append("No assistant found yet; whichever you install will "
                     "already have the telescope.")
    if failed:
        lines.append("Could not write: " + "; ".join(failed))
    return bool(done), "\n".join(lines)


def remove_all() -> tuple[bool, str]:
    """Deregister from every supported assistant."""
    results = [remove(config_path(name))[1] for name in CLIENTS]
    return True, f"Removed from {len(results)} assistant config(s)."


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
