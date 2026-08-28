"""User-only persistence for the MCP member session.

The bearer token is never returned by a tool — a token in a chat transcript
is a credential leak that cannot be recalled. It *is* written to a 0600 file
on this computer so an MCP process restart does not silently sign the member
out. Logout deletes the file.

The file is bound to the cloud origin it was issued for. Switching
TELESCOPE_MCP_CLOUD_BASE (production → staging, a custom host) must not send
the previous host's bearer token. HTTP mode never reads or writes this file:
a shared process would otherwise hand one member's login to every connector.

Override the path with TELESCOPE_MCP_SESSION_PATH for tests or unusual installs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


_DEFAULT = Path.home() / ".telescopenet" / "mcp_session"


def session_path() -> Path:
    override = os.environ.get("TELESCOPE_MCP_SESSION_PATH")
    if override:
        return Path(override)
    return _DEFAULT


def _normalise_base(base: str | None) -> str:
    return (base or "").rstrip("/")


def load(path: Path | None = None, expected_base: str | None = None) -> str | None:
    p = path or session_path()
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Legacy plaintext tokens have no origin. Refuse them rather than
        # send a production bearer to whatever TELESCOPE_MCP_CLOUD_BASE is.
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    stored_base = _normalise_base(data.get("cloud_base"))
    if not token or not stored_base:
        return None
    if expected_base is not None and stored_base != _normalise_base(expected_base):
        return None
    return str(token)


def save(token: str, path: Path | None = None, cloud_base: str | None = None) -> None:
    if not token or not _normalise_base(cloud_base):
        clear(path)
        return
    p = path or session_path()
    payload = json.dumps(
        {"cloud_base": _normalise_base(cloud_base), "token": token},
        separators=(",", ":"),
    )
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
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
