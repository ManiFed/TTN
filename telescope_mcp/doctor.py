"""Check the whole chain, and say in plain words which link is broken.

Three different faults in a row all looked identical from inside Claude: the
assistant answered from general knowledge about telescopes. The member cannot
tell those apart, and neither could the person who built it -- because when the
tools are missing, nothing we wrote is running to say so.

So the check has to live outside the assistant. It walks the chain in order,
because each link only matters if the one before it holds:

    node agent running  ->  registered with Claude  ->  the registered command
    actually starts     ->  Claude has been restarted since

and stops at the first thing that is wrong, with what to do about it.

    TelescopeNetNode --doctor
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from . import register_client
from .client import DEFAULT_AGENT_BASE

OK, BAD = "ok", "problem"


def _result(name: str, ok: bool, detail: str, fix: str = "") -> dict:
    return {"check": name, "status": OK if ok else BAD, "detail": detail,
            "fix": fix}


def _agent_running(base: str) -> dict:
    import urllib.request
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=8) as resp:
            json.load(resp)
        return _result("node software", True, "Running.")
    except Exception:
        return _result(
            "node software", False,
            "Not answering on this computer.",
            "It may still be starting -- wait a minute and run this again. If "
            "it never comes up, reinstall from thetelescope.net.")


def _registered(base: str = "") -> dict:
    path = register_client.config_path()
    if register_client.opted_out(path):
        return _result("registered with Claude", False,
                       "Deliberately opted out.",
                       f"Delete {path.parent / register_client.OPT_OUT_MARKER} "
                       f"to let the telescope register itself again.")
    data, error = register_client.load(path)
    if error:
        return _result("registered with Claude", False, error,
                       "Fix that file by hand, then run this again.")
    entry = (data.get("mcpServers") or {}).get(register_client.SERVER_KEY)
    if not entry:
        return _result(
            "registered with Claude", False,
            "Claude Desktop does not know about your telescope.",
            "Claude rewrites its own settings and sometimes drops it. The node "
            "software puts it back within five minutes -- wait, then quit and "
            "reopen Claude.")
    return _result("registered with Claude", True,
                   f"Listed in {path.name}.")


def _command_starts(base: str = "") -> dict:
    path = register_client.config_path()
    data, error = register_client.load(path)
    entry = ((data.get("mcpServers") or {}).get(register_client.SERVER_KEY)
             if not error else None)
    if not entry:
        return _result("the telescope answers Claude", False,
                       "Nothing registered to test.")

    command = entry.get("command", "")
    if not os.path.exists(command):
        return _result(
            "the telescope answers Claude", False,
            f"Claude is told to run {command}, which is not there.",
            "Reinstall from thetelescope.net -- the entry points at software "
            "that has moved or been deleted.")

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "doctor", "version": "1"}},
    })
    started = time.time()
    try:
        proc = subprocess.run([command, *entry.get("args", [])],
                              input=request + "\n", capture_output=True,
                              text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return _result("the telescope answers Claude", False,
                       "It did not answer within 90 seconds.",
                       "Reinstall from thetelescope.net.")
    first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    if not first:
        return _result(
            "the telescope answers Claude", False,
            "It started but said nothing back.",
            f"This is what Claude reports as 'server disconnected'. Last error: "
            f"{(proc.stderr or '').strip()[-200:] or 'none'}")
    try:
        json.loads(first)
    except ValueError:
        return _result("the telescope answers Claude", False,
                       "It answered with something unreadable.",
                       "Reinstall from thetelescope.net.")
    return _result("the telescope answers Claude", True,
                   f"Answered in {time.time() - started:.1f}s.")


def _elapsed_seconds(etime: str) -> float:
    """Seconds from ps `etime`, which is [[DD-]HH:]MM:SS."""
    days, _, rest = etime.rpartition("-")
    parts = [int(p) for p in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return (int(days or 0) * 86400) + hours * 3600 + minutes * 60 + seconds


def _claude_restarted(base: str = "") -> dict:
    """Whether Claude has started since the telescope was registered.

    Claude only reads its server list at startup, so an entry written while it
    is open does nothing until it is quit and reopened -- and there is no sign
    of that from inside the conversation.
    """
    path = register_client.config_path()
    if not path.exists():
        return _result("Claude restarted since", False, "Nothing registered.")
    written = path.stat().st_mtime

    running_since = None
    if platform.system() == "Darwin":
        try:
            # `etime` (D-HH:MM:SS), not `etimes` -- macOS ps has no etimes, and
            # asking for it silently drops the column rather than erroring, so
            # this check quietly answered "cannot tell" every time.
            out = subprocess.run(
                ["/bin/ps", "-Ao", "etime,comm"], capture_output=True,
                text=True, timeout=15).stdout
            for line in out.splitlines():
                if line.strip().endswith("MacOS/Claude"):
                    running_since = time.time() - _elapsed_seconds(
                        line.split()[0])
                    break
        except Exception:
            running_since = None

    if running_since is None:
        return _result(
            "Claude restarted since", True,
            "Could not tell whether Claude is running.",
            "If your telescope is missing, quit Claude completely and reopen.")
    if running_since < written:
        return _result(
            "Claude restarted since", False,
            "Claude has been open since before your telescope was registered.",
            "Quit Claude completely (Cmd-Q, not just closing the window) and "
            "open it again. It only reads its tool list when it starts.")
    return _result("Claude restarted since", True,
                   "Claude started after the telescope was registered.")


#: Names rather than functions: resolved at call time, so a replaced check --
#: in a test, or after a reload -- is the one that actually runs.
CHECK_NAMES = ("_agent_running", "_registered", "_command_starts",
               "_claude_restarted")


def run(agent_base: str = "") -> dict:
    """Walk the chain, stopping at the first broken link."""
    base = agent_base or os.environ.get("TELESCOPE_MCP_AGENT_BASE") or DEFAULT_AGENT_BASE
    results = []
    for name in CHECK_NAMES:
        check = globals()[name]
        # Every check takes the same argument, so dispatch never depends on
        # identity against a module global -- rebinding one (a test double, a
        # future refactor) would silently call the rest with the wrong shape.
        outcome = check(base)
        results.append(outcome)
        if outcome["status"] == BAD:
            # Later links cannot be judged once an earlier one is broken, and
            # four complaints teach less than one.
            break
    healthy = (all(r["status"] == OK for r in results)
               and len(results) == len(CHECK_NAMES))
    return {"healthy": healthy, "checks": results,
            "summary": _summary(results, healthy)}


def _summary(results: list, healthy: bool) -> str:
    if healthy:
        return ("Everything is connected. Open Claude Desktop and say "
                "'set up my telescope'.")
    broken = results[-1]
    return f"{broken['detail']}\n\n{broken['fix']}".strip()


def main() -> int:
    report = run()
    for entry in report["checks"]:
        mark = "ok  " if entry["status"] == OK else "FAIL"
        print(f"[{mark}] {entry['check']}: {entry['detail']}")
    print()
    print(report["summary"])
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
