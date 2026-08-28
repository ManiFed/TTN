"""User-only persistence for the MCP member session.

The bearer token is never returned by a tool — a token in a chat transcript
is a credential leak that cannot be recalled. It *is* written to a 0600 file
on this computer so an MCP process restart does not silently sign the member
out. Logout deletes the file.

Override the path with TELESCOPE_MCP_SESSION_PATH for tests or unusual installs.
"""

from __future__ import annotations

import os
from pathlib import Path


_DEFAULT = Path.home() / ".telescopenet" / "mcp_session"


def session_path() -> Path:
    override = os.environ.get("TELESCOPE_MCP_SESSION_PATH")
    if override:
        return Path(override)
    return _DEFAULT


def load(path: Path | None = None) -> str | None:
    p = path or session_path()
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def save(token: str, path: Path | None = None) -> None:
    if not token:
        clear(path)
        return
    p = path or session_path()
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
        os.chmod(p, 0o600)
    except OSError:
        # Memory token still works for this process; a restart will need
        # sign-in again, which is the previous behaviour.
        return


def clear(path: Path | None = None) -> None:
    p = path or session_path()
    try:
        p.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return
