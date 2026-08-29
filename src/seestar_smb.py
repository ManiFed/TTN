"""Seestar SMB share names, mount URLs, and watch paths.

ZWO's own docs (and Starfront's) say the S50 guest share is named
"EMMC Images", with FITS under MyWorks/. Earlier node builds mounted a
share called "seestar" at /Volumes/Seestar, which is not a share the
telescope actually exports — mount_smbfs then fails with "No such file
or directory". USB/Finder can still appear as /Volumes/Seestar; we reuse
that if it is already there rather than fighting it.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from typing import Optional


SHARE_NAME = "EMMC Images"
MYWORKS = "MyWorks"

# Volumes the Seestar app or Finder may already have mounted. Checked
# before we try our own guest mount, because /Volumes is root-owned and
# the node process cannot create a mount point there.
EXISTING_VOLUME_NAMES = (
    "EMMC Images",  # wireless SMB, as Finder labels it
    "Seestar",      # USB / Seestar app
)


def smb_share_url(host: str, *, guest: bool = True) -> str:
    """mount_smbfs URL. Spaces in the share name are percent-encoded."""
    share = quote(SHARE_NAME, safe="")
    if guest:
        return f"//guest:@{host}/{share}"
    return f"//{host}/{share}"


def cifs_unc(host: str) -> str:
    """Linux mount.cifs UNC. The share name keeps its space; it is one argv."""
    return f"//{host}/{SHARE_NAME}"


def mount_cmd(host: str, mount_point: str, system: str) -> Optional[list[str]]:
    if system == "Darwin":
        return ["mount_smbfs", "-N", smb_share_url(host, guest=True), mount_point]
    if system == "Linux":
        return [
            "mount", "-t", "cifs", cifs_unc(host), mount_point,
            "-o", "guest,uid=0,gid=0",
        ]
    return None


def find_existing_share(roots: tuple[str, ...] = ("/Volumes", "/mnt")) -> Optional[str]:
    """Return a usable already-mounted Seestar volume, or None."""
    for root in roots:
        for name in EXISTING_VOLUME_NAMES:
            path = Path(root) / name
            if _usable(path):
                return str(path)
    return None


def watch_dir(mount_root: str) -> str:
    """Point the watcher at MyWorks when the share has it."""
    myworks = Path(mount_root) / MYWORKS
    if myworks.is_dir():
        return str(myworks)
    return mount_root


def _usable(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    # A mount, or an already-populated share (MyWorks present).
    if os.path.ismount(str(path)):
        return True
    return (path / MYWORKS).is_dir()
